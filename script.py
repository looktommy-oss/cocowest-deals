#!/usr/bin/env python3
"""
Scrape CocoWest.ca for the weekly Costco flyer post vs the Weekend Update post,
display ALL items with exclusive ones highlighted, store location parsed,
interactive shopping list with clipboard export, .97 clearance prices, end dates,
and email alerts when a new weekend post drops.

Env overrides:
  MONDAY_URL, WEEKEND_URL      skip homepage discovery
  MATCH_MODE                   "smart" (default) or "userscript"
  OUTPUT_DIR                   default: public

Email alert (skipped, with a log message, if the MAIL_* values are missing):
  MAIL_USERNAME, MAIL_PASSWORD, MAIL_TO   Gmail address + app password, recipient(s)
  MAIL_HOST (smtp.gmail.com), MAIL_PORT (465)
  FORCE_NOTIFY=1               send the email even if this weekend post was already announced
  SITE_URL                     public page URL; defaults to https://<owner>.github.io/<repo>/
"""
import html
import json
import os
import random
import re
import smtplib
import ssl
import sys
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup

BASE_URL = "https://www.cocowest.ca/"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "public"))
DEBUG = os.getenv("DEBUG") == "1"
MATCH_MODE = os.getenv("MATCH_MODE", "smart").lower()

_owner, _, _repo = os.getenv("GITHUB_REPOSITORY", "").partition("/")
SITE_URL = os.getenv("SITE_URL") or (
    f"https://{_owner.lower()}.github.io/{_repo}/" if _owner and _repo else "")
if SITE_URL and not SITE_URL.endswith("/"):
    SITE_URL += "/"
STATE_URL = SITE_URL + "state.json" if SITE_URL else ""

MONDAY_RE = re.compile(os.getenv("MONDAY_RE", r"flyer"), re.I)
WEEKEND_RE = re.compile(os.getenv("WEEKEND_RE", r"weekend"), re.I)

FUZZY_THRESHOLD = 0.92

CHALLENGE_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform",
                     "attention required", "enable javascript and cookies")

def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        delay=10,
    )

def looks_like_challenge(text: str) -> bool:
    head = text[:6000].lower()
    return any(m in head for m in CHALLENGE_MARKERS)

def fetch(scraper, url: str, retries: int = 4) -> str:
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = scraper.get(
                url, timeout=45,
                headers={"Accept-Language": "en-CA,en;q=0.9", "Referer": BASE_URL},
            )
            if r.status_code == 200 and not looks_like_challenge(r.text):
                return r.text
            last = f"HTTP {r.status_code}, challenge={looks_like_challenge(r.text)}"
        except Exception as exc:
            last = repr(exc)
        wait = 2 ** attempt + random.random() * 2
        print(f"[fetch] {url} attempt {attempt}/{retries} failed ({last}); sleeping {wait:.1f}s", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"Could not fetch {url}: {last}")

SKIP_PATH_PREFIXES = ("/category/", "/tag/", "/page/", "/author/", "/wp-", "/feed",
                     "/comments", "/search", "/about", "/contact", "/privacy",
                     "/register", "/terms", "/faq", "/disclaimer")

def is_post_link(href: str) -> bool:
    u = urlparse(href)
    host = u.netloc.lower().removeprefix("www.")
    if host != urlparse(BASE_URL).netloc.lower().removeprefix("www."):
        return False
    path = u.path
    if not path.strip("/") or path.lower().startswith(SKIP_PATH_PREFIXES):
        return False
    return not re.search(r"\.(jpe?g|png|gif|webp|pdf|xml)$", path, re.I)

def discover_posts(home_html: str):
    soup = BeautifulSoup(home_html, "html.parser")
    seen, candidates = set(), []
    monday = weekend = None

    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"]).split("#")[0]
        if href in seen or not is_post_link(href):
            continue
        seen.add(href)
        blob = " ".join([a.get_text(" ", strip=True), a.get("title", ""),
                         a.get("aria-label", ""), urlparse(href).path.replace("-", " ")])
        candidates.append((href, blob[:90]))
        if weekend is None and WEEKEND_RE.search(blob):
            weekend = href
        elif monday is None and MONDAY_RE.search(blob) and not WEEKEND_RE.search(blob):
            monday = href

    monday = os.getenv("MONDAY_URL") or monday
    weekend = os.getenv("WEEKEND_URL") or weekend
    return monday, weekend

IMG_URL_RE = re.compile(
    r"^(https?:)?//\S+?\.(jpe?g|png|webp|gif|avif)(\?\S*)?$|^/\S+?\.(jpe?g|png|webp|gif|avif)(\?\S*)?$",
    re.I)
KNOWN_LAZY_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-orig-file",
                    "data-large-file", "data-full-url", "data-lazy", "data-img-src")
SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset", "srcset")
CONTAINER_SELECTORS = ("article .entry-content", ".entry-content", ".post-content",
                       ".single-content", "article", "main", "#content")

BAD_FILENAME_RE = re.compile(
    r"(logo|banner|avatar|gravatar|icon|sprite|emoji|placeholder|spinner|loader|"
    r"button|badge|social|facebook|twitter|instagram|pinterest|youtube|tiktok|"
    r"advert|sponsor|cropped-|blank|pixel|1x1|spacer|newsletter|subscribe|"
    r"next-week|what-is-on-sale|sale-next-week|promo-header)", re.I)
BAD_HOSTS = ("gravatar.com", "s.w.org", "doubleclick.net", "googlesyndication.com",
             "facebook.com", "facebook.net", "google-analytics.com")
BAD_CLASS_RE = re.compile(
    r"(sidebar|widget|comment|related|share|social|author|menu|logo|banner|"
    r"(^|[-_ ])(ads?|advert\w*|sponsor\w*)($|[-_ ])|newsletter|subscribe|breadcrumb)", re.I)
GENERIC_ALT_RE = re.compile(r"^(img|image|photo|picture|screenshot|dsc|pxl|p|untitled)?[\s_-]*\d*$", re.I)
GENERIC_NAME_RE = re.compile(r"^(img|image|photo|dsc|pxl|screenshot|untitled|p)?\d*$", re.I)

ALT_ITEM_NO_RE = re.compile(r"^\s*(\d{5,8})\b")
FILE_ITEM_NO_RE = re.compile(r"^(\d{5,8})[-_]")

@dataclass
class Item:
    url: str
    label: str
    alt: str
    alt_key: str
    file_key: str
    us_key: str
    item_no: str

def in_bad_region(el, container) -> bool:
    for parent in el.parents:
        if parent is container:
            break
        name = getattr(parent, "name", None)
        if name in (None, "[document]", "html", "body"):
            continue
        if name in ("nav", "footer", "aside"):
            return True
        if name == "header" and not parent.find_parent("article"):
            return True
        classes = " ".join(parent.get("class", []))
        pid = parent.get("id", "") or ""
        if (classes or pid) and BAD_CLASS_RE.search(f"{classes} {pid}"):
            return True
    return False

def largest_from_srcset(value: str):
    pairs = re.findall(r"(\S+)\s+(\d+)w", value)
    if pairs:
        return max(pairs, key=lambda p: int(p[1]))[0]
    return value.split(",")[0].strip().split(" ")[0] or None

def usable(url) -> bool:
    return bool(url) and not url.startswith("data:") and not re.search(
        r"(placeholder|blank\.gif|1x1|lazy[-_]?load|spinner)", url, re.I)

def candidate_urls(img):
    out = []
    parent = img.find_parent("a", href=True)
    if parent and IMG_URL_RE.match(parent["href"]):
        out.append(parent["href"])
    for attr in SRCSET_ATTRS:
        if img.get(attr):
            out.append(largest_from_srcset(img[attr]))
    for attr in KNOWN_LAZY_ATTRS:
        out.append(img.get(attr))
    for attr, val in img.attrs.items():
        if attr.startswith("data-") and isinstance(val, str) and IMG_URL_RE.match(val.strip()):
            out.append(val.strip())
    out.append(img.get("src"))
    return [u.strip() for u in out if isinstance(u, str) and usable(u.strip())]

def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def filename_key(url: str):
    name = Path(unquote(urlparse(url).path)).stem.lower()
    prev = None
    while prev != name:
        prev = name
        name = re.sub(r"-\d{2,4}x\d{2,4}$", "", name)
        name = re.sub(r"-(scaled|rotated|edited|copy|min|large|medium|thumbnail)$", "", name)
        name = re.sub(r"[-_]\d{1,2}$", "", name)
    key = re.sub(r"[^a-z0-9]", "", name)
    return key if len(key) >= 5 and not GENERIC_NAME_RE.match(key) else ""

def pretty_name(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).stem
    name = re.sub(r"-\d{2,4}x\d{2,4}$", "", name)
    return re.sub(r"[-_]+", " ", name).strip().title()

def userscript_key(img) -> str:
    alt = img.get("alt") or ""
    if alt:
        return alt
    src = img.get("src") or ""
    return src.split("?")[0].rsplit("/", 1)[-1]

def find_item_no(alt: str, url: str) -> str:
    m = ALT_ITEM_NO_RE.match(alt or "")
    if m:
        return m.group(1)
    m = FILE_ITEM_NO_RE.match(Path(unquote(urlparse(url).path)).name)
    return m.group(1) if m else ""

def reject_reason(img, url: str, container):
    u = urlparse(url)
    fig = img.find_parent("figure")
    cap = fig.find("figcaption") if fig else None
    cap_text = cap.get_text(" ", strip=True) if cap else ""
    blob = " ".join([
        img.get("alt") or "",
        img.get("title") or "",
        cap_text,
        Path(u.path).name
    ]).lower()

    if "what is on sale next week" in blob or "sales from last year" in blob:
        return "promo_banner"
    if any(h in u.netloc.lower() for h in BAD_HOSTS):
        return "bad_host"
    if u.path.lower().endswith((".svg", ".ico")):
        return "svg_or_ico"
    if BAD_FILENAME_RE.search(Path(u.path).name):
        return "bad_filename"
    try:
        w, h = int(img.get("width", 0)), int(img.get("height", 0))
        if (w and w < 120) or (h and h < 120):
            return "too_small"
    except ValueError:
        pass
    if in_bad_region(img, container):
        return "bad_region"
    return None

def extract_items(post_html: str, page_url: str):
    soup = BeautifulSoup(post_html, "html.parser")
    for sel in CONTAINER_SELECTORS:
        el = soup.select_one(sel)
        if el is not None and el.find("img"):
            items, _ = extract_from(el, page_url)
            if items:
                return items
    items, _ = extract_from(soup, page_url)
    return items

def extract_from(container, page_url: str):
    imgs = list(container.find_all("img"))
    for ns in container.find_all("noscript"):
        if ns.string:
            imgs.extend(BeautifulSoup(ns.string, "html.parser").find_all("img"))
    items, seen = [], set()
    for img in imgs:
        cands = candidate_urls(img)
        if not cands:
            continue
        url = urljoin(page_url, cands[0])
        if reject_reason(img, url, container):
            continue
        alt = (img.get("alt") or img.get("title") or "").strip()
        if not alt:
            fig = img.find_parent("figure")
            cap = fig.find("figcaption") if fig else None
            alt = cap.get_text(" ", strip=True) if cap else ""
        good_alt = len(alt) >= 4 and not GENERIC_ALT_RE.match(alt)
        alt_key = norm_text(alt) if good_alt else ""
        file_key = filename_key(url)
        item_no = find_item_no(alt, url)
        dedupe_key = f"no:{item_no}" if item_no else (file_key or alt_key or url)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(Item(url=url, label=alt if good_alt else pretty_name(url),
                          alt=alt, alt_key=alt_key, file_key=file_key,
                          us_key=userscript_key(img), item_no=item_no))
    return items, imgs

def extract_location(post_html: str) -> str:
    soup = BeautifulSoup(post_html, "html.parser")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Location:\s*([A-Za-z0-9\s\(\),_-]+?)(?=\.\s|Hi\s|Hello\s|$)", text, re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"Location:\s*([^<\n\r]+)", post_html, re.I)
    if m2:
        return m2.group(1).strip().split("<")[0]
    return ""

def find_exclusive(weekend_items, monday_items):
    mon_nos = {i.item_no for i in monday_items if i.item_no}
    mon_files = {i.file_key for i in monday_items if i.file_key}
    mon_alts = [i.alt_key for i in monday_items if i.alt_key]
    mon_alt_set = set(mon_alts)

    def duplicate(it: Item) -> bool:
        if it.item_no:
            return it.item_no in mon_nos or (bool(it.file_key) and it.file_key in mon_files)
        if it.file_key and it.file_key in mon_files:
            return True
        if it.alt_key:
            if it.alt_key in mon_alt_set:
                return True
            if len(it.alt_key) >= 12 and any(
                    SequenceMatcher(None, it.alt_key, m).ratio() >= FUZZY_THRESHOLD
                    for m in mon_alts):
                return True
        return False

    return [i for i in weekend_items if not duplicate(i)]

def find_exclusive_userscript(weekend_items, monday_items):
    mon = {i.us_key for i in monday_items if i.us_key}
    return [i for i in weekend_items if i.us_key and i.us_key not in mon]


# Price and end-date parsing
PRICE_RE = re.compile(r"\$\s?(\d[\d,]*)\.(\d{2})\b")
END_RE = re.compile(
    r"(?:EXPIRES|ENDS|VALID\s+(?:UNTIL|THROUGH|THRU))\s+(?:ON\s+)?(\d{4}-\d{2}-\d{2})", re.I)

def parse_price(label: str):
    text = re.sub(r"\([^)]*\)", " ", label or "")
    found = PRICE_RE.findall(text)
    return found[-1] if found else None

def is_clearance(label: str) -> bool:
    p = parse_price(label)
    return bool(p) and p[1] == "97"

def parse_end_date(label: str) -> str:
    m = END_RE.search(label or "")
    if not m:
        return ""
    try:
        datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return ""
    return m.group(1)


# State and alerting
def load_previous_state(scraper):
    if not STATE_URL:
        print("[state] no site URL known; skipping state check")
        return {}, False
    try:
        r = scraper.get(f"{STATE_URL}?t={int(time.time())}", timeout=30)
        if r.status_code == 200:
            return r.json(), True
        if r.status_code == 404:
            return {}, True
    except Exception as exc:
        print(f"[state] could not load previous state: {exc!r}", file=sys.stderr)
    return {}, False

def send_email(subject: str, body: str) -> bool:
    user = os.getenv("MAIL_USERNAME")
    password = os.getenv("MAIL_PASSWORD")
    to = os.getenv("MAIL_TO")
    if not (user and password and to):
        print("[email] skipped: MAIL_* secrets not set.")
        return False

    recipients = [e.strip() for e in to.split(",") if e.strip()]
    host = os.getenv("MAIL_HOST", "smtp.gmail.com")
    port = int(os.getenv("MAIL_PORT", "465"))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg, to_addrs=recipients)
        print(f"[email] sent to {', '.join(recipients)}")
        return True
    except Exception as exc:
        print(f"[email] failed: {exc!r}", file=sys.stderr)
    return False

def maybe_notify(prev_state, state_ok, weekend_url, monday_url, weekend_items, exclusive):
    force = os.getenv("FORCE_NOTIFY") == "1"
    is_new_post = weekend_url != prev_state.get("weekend_url")
    if not (force or (is_new_post and state_ok)):
        print("[email] not sending notification (already announced or state unreachable)")
        return

    lines = [
        f"CocoWest weekend deals updated: {len(weekend_items)} items in the weekend post, "
        f"{len(exclusive)} new exclusives.",
        f"Browse them: {SITE_URL or '(site URL unknown)'}",
        f"Weekend post: {weekend_url}"
    ]
    send_email(f"CocoWest: {len(exclusive)} new weekend deals", "\n".join(lines))


# Dashboard HTML Template
PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1b8a3a">
<title>Costco Deals Dashboard</title>
<style>
  :root {
    --bg:#f4f5f7; --fg:#1a1a1a; --card:#ffffff; --muted:#666; --line:#e2e2e2;
    --accent:#1b8a3a; --on-accent:#ffffff; --bar:rgba(255,255,255,.94);
    --highlight:#e6f4ea; --highlight-border:#34a853; --danger:#dc3545;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#121212; --fg:#eeeeee; --card:#1e1e1e; --muted:#9a9a9a; --line:#333;
            --accent:#3ddc6a; --on-accent:#08210f; --bar:rgba(18,18,18,.94);
            --highlight:#132618; --highlight-border:#28a745; }
  }
  * { box-sizing:border-box; }
  [hidden] { display:none !important; }
  body { margin:0; background:var(--bg); color:var(--fg); font-family:system-ui,-apple-system,sans-serif; padding-bottom:3rem; }
  .bar { position:sticky; top:0; z-index:10; background:var(--bar); backdrop-filter:blur(8px); border-bottom:1px solid var(--line); padding:.6rem .8rem; }
  .bar-row { max-width:1200px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; gap:.6rem; }
  .title { font-size:1.05rem; font-weight:700; }
  .badge-btn { background:var(--accent); color:var(--on-accent); border:none; border-radius:999px; padding:.35rem .85rem; font-size:.9rem; font-weight:700; cursor:pointer; white-space:nowrap; }
  .badge-btn.gray { background:#6c757d; color:#fff; }
  .badge-btn.active { background:var(--danger); color:#fff; }
  .search { max-width:1200px; margin:.5rem auto 0; display:flex; gap:.5rem; }
  .search input { flex:1; min-width:0; padding:.6rem .8rem; font-size:16px; border:1px solid var(--line); border-radius:10px; background:var(--card); color:var(--fg); }
  .toolbar { max-width:1200px; margin:.6rem auto 0; padding:0 .8rem; display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; }
  .chip { border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:999px; padding:.3rem .75rem; font-size:.85rem; font-weight:600; cursor:pointer; }
  .chip.active { background:var(--accent); color:var(--on-accent); border-color:var(--accent); }
  .chip.danger { color:var(--danger); border-color:var(--danger); }
  .chip.info { background:#007bff; color:#fff; border-color:#007bff; }
  .cols-toggle { display:flex; align-items:center; gap:.35rem; margin-left:auto; font-size:.8rem; color:var(--muted); }
  .seg { display:inline-flex; border:1px solid var(--line); border-radius:999px; overflow:hidden; }
  .seg button { border:none; background:var(--card); color:var(--fg); padding:.3rem .7rem; font-size:.85rem; font-weight:700; cursor:pointer; }
  .seg button.active { background:var(--accent); color:var(--on-accent); }
  .meta { max-width:1200px; margin:.6rem auto .2rem; padding:0 .8rem; color:var(--muted); font-size:.8rem; line-height:1.5; }
  .meta a { color:inherit; }
  .grid { max-width:1200px; margin:.6rem auto; padding:0 .8rem; display:grid; gap:.8rem; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); }
  .grid.c1 { grid-template-columns:1fr; }
  .grid.c2 { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .card { margin:0; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; position:relative; display:flex; flex-direction:column; justify-content:space-between; }
  .card.is-new { border:2px solid var(--highlight-border); background:var(--highlight); }
  .card.is-expired img, .card.is-expired figcaption { opacity:.5; }
  .new-tag { position:absolute; top:8px; left:8px; background:#28a745; color:#fff; font-size:.7rem; font-weight:bold; padding:2px 6px; border-radius:4px; z-index:2; text-transform:uppercase; }
  .clr-tag { position:absolute; top:8px; right:8px; background:#6f42c1; color:#fff; font-size:.7rem; font-weight:bold; padding:2px 6px; border-radius:4px; z-index:2; }
  .card a { display:block; }
  .card img { width:100%; aspect-ratio:1/1; object-fit:contain; display:block; background:#fff; }
  .card figcaption { padding:.5rem .6rem; font-size:.85rem; line-height:1.3; flex-grow:1; overflow-wrap:anywhere; }
  .card-footer { padding:.4rem .6rem .6rem; display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:.3rem; border-top:1px solid var(--line); font-size:.8rem; }
  .shop-label { display:flex; align-items:center; gap:.4rem; cursor:pointer; font-weight:500; }
  .shop-label input { width:1.1rem; height:1.1rem; accent-color:var(--accent); cursor:pointer; }
  .end-badge { font-size:.72rem; font-weight:700; padding:2px 8px; border-radius:999px; white-space:nowrap; }
  .end-badge.today { background:#dc3545; color:#fff; }
  .end-badge.tomorrow { background:#fd7e14; color:#fff; }
  .end-badge.week { background:#ffc107; color:#1a1a1a; }
  .end-badge.later { background:transparent; color:var(--muted); border:1px solid var(--line); font-weight:600; }
  .end-badge.expired { background:var(--line); color:var(--muted); text-decoration:line-through; }
  .empty { padding:2rem 1rem; text-align:center; color:var(--muted); grid-column:1/-1; }
  @media (max-width:640px) {
    .toolbar { padding:0 .6rem; }
    .grid { padding:0 .5rem; gap:.5rem; }
  }
</style>
</head>
<body>
<header class="bar">
  <div class="bar-row">
    <div class="title">&#128717;&#65039; Costco Deals</div>
    <div style="display:flex; gap:.4rem;">
      <button class="badge-btn gray" id="copy-btn" type="button" title="Copy checked list">&#128203; Copy</button>
      <button class="badge-btn" id="list-toggle" type="button">&#128722; List (0)</button>
    </div>
  </div>
  <div class="search">
    <input id="q" type="search" placeholder="Filter by name or item number..." autocomplete="off">
    <button class="badge-btn gray" id="reset-btn" type="button">Reset</button>
  </div>
</header>
<div class="toolbar">
  <button class="chip info" id="source-toggle" type="button">Showing: Weekend Update</button>
  <button class="chip" id="new-toggle" type="button" aria-pressed="false">&#10024; New only (__NEW_COUNT__)</button>
  <button class="chip" id="clr-toggle" type="button" aria-pressed="false">&#127991;&#65039; .97 clearance (__CLR_COUNT__)</button>
  <button class="chip" id="week-toggle" type="button" aria-pressed="false">&#9200; Ends this week (0)</button>
  <button class="chip danger" id="clear-list" type="button" hidden>Clear list</button>
  <div class="cols-toggle">
    <span>Per row</span>
    <span class="seg">
      <button type="button" data-cols="1" aria-pressed="false">1</button>
      <button type="button" data-cols="2" aria-pressed="true">2</button>
    </span>
  </div>
</div>
<div class="meta">
  &#128205; Store Location: <b>__LOCATION__</b><br>
  Showing <b id="shown-count">__TOTAL__</b> items &middot; updated __STAMP__<br>
  Sources: <a href="__FLYER_URL__">Monday Flyer</a> &middot; <a href="__WEEKEND_URL__">Weekend Update</a>
</div>
<main class="grid c2" id="grid">
__CARDS__
<p class="empty" id="no-match" hidden></p>
</main>
<script>
(function () {
  try {
    var LIST_KEY = 'cocowest_shopping_list';
    var COLS_KEY = 'cocowest_cols';
    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    function load(key, fallback) {
      try {
        var raw = localStorage.getItem(key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (e) { return fallback; }
    }
    function save(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
    }

    function classifyEnd(s, today) {
      var p = s.split('-');
      var end = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
      var left = Math.round((end - today) / 86400000);
      var daysToSunday = (7 - today.getDay()) % 7;
      var short = MONTHS[end.getMonth()] + ' ' + end.getDate();
      if (left < 0) return { cls: 'expired', text: 'Expired ' + short, inWeek: false, expired: true };
      if (left > daysToSunday) return { cls: 'later', text: 'Ends ' + short, inWeek: false, expired: false };
      if (left === 0) return { cls: 'today', text: 'Ends today', inWeek: true, expired: false };
      if (left === 1) return { cls: 'tomorrow', text: 'Ends tomorrow', inWeek: true, expired: false };
      return { cls: 'week', text: 'Ends ' + DAYS[end.getDay()], inWeek: true, expired: false };
    }

    var q = document.getElementById('q');
    var grid = document.getElementById('grid');
    var listBtn = document.getElementById('list-toggle');
    var copyBtn = document.getElementById('copy-btn');
    var sourceBtn = document.getElementById('source-toggle');
    var newBtn = document.getElementById('new-toggle');
    var clrBtn = document.getElementById('clr-toggle');
    var weekBtn = document.getElementById('week-toggle');
    var clearBtn = document.getElementById('clear-list');
    var resetBtn = document.getElementById('reset-btn');
    var shownEl = document.getElementById('shown-count');
    var emptyMsg = document.getElementById('no-match');
    var colBtns = Array.prototype.slice.call(document.querySelectorAll('[data-cols]'));
    var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
    var checks = Array.prototype.slice.call(document.querySelectorAll('.shop-check'));

    var shoppingList = load(LIST_KEY, []).map(Number);
    if (!Array.isArray(shoppingList)) shoppingList = [];
    var state = { listOnly: false, newOnly: false, clrOnly: false, weekOnly: false, viewingMonday: false };

    var now = new Date();
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    cards.forEach(function (el) {
      var s = el.getAttribute('data-end');
      var badge = el.querySelector('.end-badge');
      if (!s || !badge) return;
      var r = classifyEnd(s, today);
      badge.className = 'end-badge ' + r.cls;
      badge.textContent = r.text;
      badge.hidden = false;
      if (r.expired) el.classList.add('is-expired');
      if (r.inWeek) el.setAttribute('data-week', '1');
    });

    function inList(idx) { return shoppingList.indexOf(Number(idx)) !== -1; }
    function listCountOnPage() {
      return cards.filter(function (el) {
        var idx = Number(el.getAttribute('data-index'));
        var isMonday = el.classList.contains('is-monday');
        var matchesSource = state.viewingMonday ? isMonday : !isMonday;
        return matchesSource && inList(idx);
      }).length;
    }

    function render() {
      var t = q.value.trim().toLowerCase();
      var shown = 0;
      cards.forEach(function (el) {
        var idx = Number(el.getAttribute('data-index'));
        var isMonday = el.classList.contains('is-monday');
        var matchesSource = state.viewingMonday ? isMonday : !isMonday;
        var ok = matchesSource &&
                 (!t || el.getAttribute('data-t').indexOf(t) !== -1) &&
                 (!state.newOnly || el.classList.contains('is-new')) &&
                 (!state.clrOnly || el.classList.contains('is-clr')) &&
                 (!state.weekOnly || el.getAttribute('data-week') === '1') &&
                 (!state.listOnly || inList(idx));
        el.hidden = !ok;
        if (ok) shown++;
      });

      var n = listCountOnPage();
      listBtn.innerHTML = state.listOnly ? 'Show all items' : '&#128722; List (' + n + ')';
      listBtn.classList.toggle('active', state.listOnly);
      sourceBtn.textContent = state.viewingMonday ? 'Showing: Monday Flyer' : 'Showing: Weekend Update';
      newBtn.style.opacity = state.viewingMonday ? '0.4' : '1';
      newBtn.disabled = state.viewingMonday;
      
      [[clrBtn, state.clrOnly], [weekBtn, state.weekOnly]].forEach(function (pair) {
        pair[0].classList.toggle('active', pair[1]);
        pair[0].setAttribute('aria-pressed', pair[1] ? 'true' : 'false');
      });
      clearBtn.hidden = n === 0;
      clearBtn.textContent = 'Clear list (' + n + ')';
      shownEl.textContent = shown;

      if (shown === 0) {
        emptyMsg.textContent = 'No items match your filters.';
      }
      emptyMsg.hidden = shown !== 0;
    }

    checks.forEach(function (chk) {
      var idx = Number(chk.getAttribute('data-index'));
      chk.checked = inList(idx);
      chk.addEventListener('change', function () {
        if (chk.checked) {
          if (!inList(idx)) shoppingList.push(idx);
        } else {
          shoppingList = shoppingList.filter(function (i) { return i !== idx; });
        }
        save(LIST_KEY, shoppingList);
        render();
      });
    });

    sourceBtn.addEventListener('click', function () {
      state.viewingMonday = !state.viewingMonday;
      if (state.viewingMonday) state.newOnly = false;
      render();
    });
    listBtn.addEventListener('click', function () { state.listOnly = !state.listOnly; render(); });
    newBtn.addEventListener('click', function () { if (!state.viewingMonday) { state.newOnly = !state.newOnly; render(); } });
    clrBtn.addEventListener('click', function () { state.clrOnly = !state.clrOnly; render(); });
    weekBtn.addEventListener('click', function () { state.weekOnly = !state.weekOnly; render(); });
    q.addEventListener('input', render);

    copyBtn.addEventListener('click', function () {
      if (shoppingList.length === 0) {
        alert('Your shopping list is empty!');
        return;
      }
      var itemsText = [];
      cards.forEach(function (el) {
        var idx = Number(el.getAttribute('data-index'));
        if (inList(idx)) {
          var label = el.querySelector('figcaption').textContent.trim();
          itemsText.push('- ' + label);
        }
      });
      navigator.clipboard.writeText("Costco Shopping List:\n" + itemsText.join('\n')).then(function () {
        alert('Shopping list copied to clipboard!');
      }).catch(function (err) {
        alert('Failed to copy: ' + err);
      });
    });

    resetBtn.addEventListener('click', function () {
      q.value = '';
      state.newOnly = false;
      state.clrOnly = false;
      state.weekOnly = false;
      state.listOnly = false;
      state.viewingMonday = false;
      render();
    });

    clearBtn.addEventListener('click', function () {
      var n = listCountOnPage();
      if (!n) return;
      if (!window.confirm('Remove all ' + n + ' items from your shopping list?')) return;
      shoppingList = shoppingList.filter(function (idx) {
        var el = document.querySelector('[data-index="' + idx + '"]');
        var isMonday = el && el.classList.contains('is-monday');
        var matchesSource = state.viewingMonday ? isMonday : !isMonday;
        return !matchesSource; // keep items not currently viewed
      });
      save(LIST_KEY, shoppingList);
      checks.forEach(function (chk) {
        var idx = Number(chk.getAttribute('data-index'));
        chk.checked = inList(idx);
      });
      render();
    });

    function setCols(n) {
      n = (n === 1) ? 1 : 2;
      grid.classList.remove('c1', 'c2');
      grid.classList.add('c' + n);
      colBtns.forEach(function (b) {
        var on = Number(b.getAttribute('data-cols')) === n;
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      save(COLS_KEY, n);
    }
    colBtns.forEach(function (b) {
      b.addEventListener('click', function () { setCols(Number(b.getAttribute('data-cols'))); });
    });
    setCols(Number(load(COLS_KEY, 2)));

    render();
  } catch (err) {
    alert("Script Error: " + err.message);
  }
})();
</script>
</body>
</html>"""

def render_html(weekend_items, monday_items, exclusive_urls, monday_url, weekend_url, store_location) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    esc = html.escape
    new_count = len(exclusive_urls)
    all_items_for_clr = list(weekend_items) + list(monday_items)
    clr_count = sum(1 for i in all_items_for_clr if is_clearance(i.label))

    cards = []
    idx = 0
    # Render Weekend Items
    for i in weekend_items:
        is_new = i.url in exclusive_urls
        clearance = is_clearance(i.label)
        end = parse_end_date(i.label)

        classes = ["card"]
        if is_new:
            classes.append("is-new")
        if clearance:
            classes.append("is-clr")

        safe_label = re.sub(r'[\r\n]+', ' ', i.label).strip()
        safe_label_lower = safe_label.lower()

        attrs = f'data-index="{idx}" data-t="{esc(safe_label_lower, quote=True)}" data-url="{esc(i.url, quote=True)}"'
        if end:
            attrs += f' data-end="{end}"'

        new_tag = '<div class="new-tag">&#10024; New</div>' if is_new else ''
        clr_tag = '<div class="clr-tag">&#127991;&#65039; .97</div>' if clearance else ''
        end_badge = '<span class="end-badge" hidden></span>' if end else ''

        cards.append(f'''<figure class="{' '.join(classes)}" {attrs}>
  {new_tag}{clr_tag}
  <a href="{esc(i.url, quote=True)}" target="_blank" rel="noopener">
    <img src="{esc(i.url, quote=True)}" alt="{esc(safe_label, quote=True)}" loading="lazy" referrerpolicy="no-referrer">
  </a>
  <figcaption>{esc(safe_label)}</figcaption>
  <div class="card-footer">
    <label class="shop-label">
      <input type="checkbox" class="shop-check" data-index="{idx}"> Add to list
    </label>
    {end_badge}
  </div>
</figure>''')
        idx += 1

    # Render Monday Items
    for i in monday_items:
        clearance = is_clearance(i.label)
        end = parse_end_date(i.label)

        classes = ["card", "is-monday"]
        if clearance:
            classes.append("is-clr")

        safe_label = re.sub(r'[\r\n]+', ' ', i.label).strip()
        safe_label_lower = safe_label.lower()

        attrs = f'data-index="{idx}" data-t="{esc(safe_label_lower, quote=True)}" data-url="{esc(i.url, quote=True)}"'
        if end:
            attrs += f' data-end="{end}"'

        clr_tag = '<div class="clr-tag">&#127991;&#65039; .97</div>' if clearance else ''
        end_badge = '<span class="end-badge" hidden></span>' if end else ''

        cards.append(f'''<figure class="{' '.join(classes)}" {attrs}>
  {clr_tag}
  <a href="{esc(i.url, quote=True)}" target="_blank" rel="noopener">
    <img src="{esc(i.url, quote=True)}" alt="{esc(safe_label, quote=True)}" loading="lazy" referrerpolicy="no-referrer">
  </a>
  <figcaption>{esc(safe_label)}</figcaption>
  <div class="card-footer">
    <label class="shop-label">
      <input type="checkbox" class="shop-check" data-index="{idx}"> Add to list
    </label>
    {end_badge}
  </div>
</figure>''')
        idx += 1

    cards_html = "\n".join(cards) or '<p class="empty">No items found.</p>'

    return (PAGE_TEMPLATE
            .replace("__TOTAL__", str(len(weekend_items)))
            .replace("__NEW_COUNT__", str(new_count))
            .replace("__CLR_COUNT__", str(clr_count))
            .replace("__LOCATION__", esc(store_location or "Metro Vancouver Area"))
            .replace("__STAMP__", stamp)
            .replace("__FLYER_URL__", esc(monday_url, quote=True))
            .replace("__WEEKEND_URL__", esc(weekend_url, quote=True))
            .replace("__CARDS__", cards_html))

def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    scraper = make_scraper()

    monday_url, weekend_url = os.getenv("MONDAY_URL"), os.getenv("WEEKEND_URL")
    if not (monday_url and weekend_url):
        home = fetch(scraper, BASE_URL)
        monday_url, weekend_url = discover_posts(home)
    if not (monday_url and weekend_url):
        print("ERROR: could not find both posts.", file=sys.stderr)
        return 1

    monday_html = fetch(scraper, monday_url)
    time.sleep(random.uniform(1, 3))
    weekend_html = fetch(scraper, weekend_url)

    monday_items = extract_items(monday_html, monday_url)
    weekend_items = extract_items(weekend_html, weekend_url)

    if not monday_items or not weekend_items:
        print("ERROR: zero images extracted; refusing to deploy.", file=sys.stderr)
        return 1

    store_location = extract_location(weekend_html) or extract_location(monday_html)

    exclusive = find_exclusive_userscript(weekend_items, monday_items) if MATCH_MODE == "userscript" else find_exclusive(weekend_items, monday_items)
    exclusive_urls = {i.url for i in exclusive}

    print(f"Location: {store_location}")
    print(f"Total weekend items: {len(weekend_items)} | Monday items: {len(monday_items)} | New exclusives: {len(exclusive_urls)}")

    prev_state, state_ok = load_previous_state(scraper)

    (OUTPUT_DIR / "index.html").write_text(
        render_html(weekend_items, monday_items, exclusive_urls, monday_url, weekend_url, store_location),
        encoding="utf-8")
    (OUTPUT_DIR / "state.json").write_text(json.dumps({
        "weekend_url": weekend_url,
        "flyer_url": monday_url,
        "total_count": len(weekend_items),
        "exclusive_count": len(exclusive),
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")

    maybe_notify(prev_state, state_ok, weekend_url, monday_url, weekend_items, exclusive)
    return 0

if __name__ == "__main__":
    sys.exit(main())
