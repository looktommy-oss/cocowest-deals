#!/usr/bin/env python3
"""
Scrape CocoWest.ca for Costco posts with robust category tagging and filtering,
smart date-aware post discovery, Vancouver time stamps, shopping list, store location, clearance tags, and end dates.

Env overrides:
  MONDAY_URL, WEEKEND_URL      skip homepage discovery
  MATCH_MODE                   "smart" (default) or "userscript"
  OUTPUT_DIR                   default: public
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
from zoneinfo import ZoneInfo
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

WEEKEND_RE = re.compile(os.getenv("WEEKEND_RE", r"weekend"), re.I)
IGNORE_RE = re.compile(r"(seasonal|electronics|food[-_\s]review|review|recall|contest|clothing|executive|black[-_\s]friday|boxing[-_\s]day)", re.I)

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
                     "-review/", "/register", "/terms", "/faq", "/disclaimer")

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
    seen = set()
    all_posts = []

    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"]).split("#")[0]
        if href in seen or not is_post_link(href):
            continue
        seen.add(href)
        blob = " ".join([a.get_text(" ", strip=True), a.get("title", ""),
                         a.get("aria-label", ""), urlparse(href).path.replace("-", " ")])
        all_posts.append((href, blob))

    weekend_url = os.getenv("WEEKEND_URL")
    monday_url = os.getenv("MONDAY_URL")

    if not weekend_url:
        for href, blob in all_posts:
            if WEEKEND_RE.search(blob):
                weekend_url = href
                break

    if not monday_url:
        for href, blob in all_posts:
            if href == weekend_url:
                continue
            if IGNORE_RE.search(blob):
                continue
            monday_url = href
            break

    if not monday_url and all_posts:
        monday_url = all_posts[0][0]

    return monday_url, weekend_url

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
    category: str

CATEGORY_RULES = {
    "groceries": re.compile(r"\b(coffee|protein|snack|meat|chicken|beef|pork|salmon|shrimp|bacon|sausage|juice|water|milk|cheese|chips|chocolate|cookie|bar|almond|rice|pasta|sauce|soup|oil|dressing|bread|cereal|yogurt|fruit|vegetable|salad|pizza|dumpling|egg|butter|nuts|honey|syrup|tea|wine|beer|spirits|kirkland|granola|tortilla|salsa|frozen|organic|crackers|oats|soda|coke|pepsi|torani|croissant|muffin|pie|cake|deli|ham|turkey|salami|dip|hummus|guacamole|tuna|tofu|broth|ketchup|mayo|mustard|spices|seasoning|strudel|strudels|chia|apple|pork|steak|ribs|wings|burger|hotdog|noodle|ramen|tortellini|ravioli|ice\s*cream|gelato|sorbet|sparkling|lemonade|energy\s*drink|gatorade|electrolyte|pretzels|popcorn|cashew|pistachio|walnut|pecan|raisin|cranberry)\b", re.I),
    "appliances": re.compile(r"\b(toaster|blender|espresso|air\s*fryer|microwave|mixer|cookware|pan|pot|kettle|vacuum|shaver|toothbrush|fan|heater|grill|cooker|fridge|freezer|iron|steamer|ice\s*maker|juicer|food\s*processor|hair\s*dryer|curling|pressure\s*cooker|slow\s*cooker|deep\s*fryer|panini\s*press|waffle\s*maker)\b", re.I),
    "electronics": re.compile(r"\b(tv|television|monitor|speaker|headphone|earbud|soundbar|sound\s*bar|laptop|tablet|phone|iphone|galaxy|ipad|macbook|charger|cable|router|camera|smart\s*watch|projector|receiver|hdmi|usb|hub|airpods?|samsung|sony|bose|jbl|battery|batteries|duracell|energizer|surge|powerbank|power\s*bank|webcam|microsd|sd\s*card|fire\s*stick|roku|chromecast|apple\s*tv|nintendo|playstation|xbox)\b", re.I),
    "household": re.compile(r"\b(paper\s*towel|toilet\s*paper|tissues?|facial\s*tissues?|detergent|dish\s*soap|trash\s*bags?|garbage\s*bags?|foil|ziploc|sponge|cleaner|disinfectant|light\s*bulb|napkins?|wipes?|laundry|fabric\s*softener|storage\s*box|container|bag|wrap|tide|gain|kirkland\s*signature\s*bath|kirkland\s*signature\s*paper|kirkland\s*signature\s*foil|bleach|lysol|clorox|febreze|trash\s*can|garbage\s*can)\b", re.I),
    "clothing": re.compile(r"\b(pants?|jeans|hoodie|sweatshirt|socks?|jacket|coat|shirt|shorts?|sweater|underwear|shoes?|boots?|slippers?|gloves?|hat|leggings?|t-shirt|belt|pyjamas?|tee|puma|adidas|nike|ck|calvin|levi|matrix|bamboo|activewear|bra|boxers|briefs|pj|under\s*armour|sketchers|new\s*balance|champion|pullover|cargo|joggers?)\b", re.I),
    "health": re.compile(r"\b(vitamin|supplement|shampoo|conditioner|lotion|soap|toothpaste|pain\s*relief|advil|tylenol|multivitamin|collagen|mouthwash|deodorant|razor|sunscreen|eye\s*drops?|floss|skin\s*care|cream|bandage|aspirin|aleve|metamucil|magnesium|omega|b12|d3|probiotic|body\s*wash|hair\s*growth|ibuprofen|acetaminophen|allergy|zyrtec|claritin|band-aid|first\s*aid|protein\s*powder|creatine)\b", re.I),
    "home": re.compile(r"\b(pillow|sheet|blanket|duvet|towel|mat|hanger|storage|bin|basket|lamp|rug|mattress|curtain|candle|diffuser|desk|chair|furniture|pad|comforter|quilt|sheets|bath\s*sheet|bath\s*towel|organizer|throw|mattress\s*pad|mattress\s*topper|bed\s*sheet|duvet\s*cover|memory\s*foam|nightstand|bookshelf|frame)\b", re.I),
}

def detect_category(label: str) -> str:
    for cat, pattern in CATEGORY_RULES.items():
        if pattern.search(label or ""):
            return cat
    return "other"

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

def extract_from(container, page_url: str, stats: Counter):
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
        label = alt if good_alt else pretty_name(url)
        alt_key = norm_text(alt) if good_alt else ""
        file_key = filename_key(url)
        item_no = find_item_no(alt, url)
        category = detect_category(label)
        dedupe_key = f"no:{item_no}" if item_no else (file_key or alt_key or url)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(Item(url=url, label=label, alt=alt, alt_key=alt_key, 
                          file_key=file_key, us_key=userscript_key(img), 
                          item_no=item_no, category=category))
    return items, imgs

def extract_items(post_html: str, page_url: str):
    soup = BeautifulSoup(post_html, "html.parser")
    for sel in CONTAINER_SELECTORS:
        el = soup.select_one(sel)
        if el is not None and el.find("img"):
            items, _ = extract_from(el, page_url, Counter())
            if items:
                return items
    items, _ = extract_from(soup, page_url, Counter())
    return items

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


# ------------------------------------------------- price + end-date parsing

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


# --------------------------------------------------------- state + alerting

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

def maybe_notify(prev_state, state_ok, target_url, is_weekend_mode, items_count, exclusive_count):
    force = os.getenv("FORCE_NOTIFY") == "1"
    key = "weekend_url" if is_weekend_mode else "flyer_url"
    is_new_post = target_url != prev_state.get(key)
    if not (force or (is_new_post and state_ok)):
        print("[email] not sending notification")
        return

    mode_name = "Weekend Exclusives" if is_weekend_mode else "Monday Sales Post"
    lines = [
        f"CocoWest {mode_name} updated: {items_count} items found.",
        f"Browse them: {SITE_URL or '(site URL unknown)'}",
        f"Post URL: {target_url}"
    ]
    subj = f"CocoWest: {exclusive_count} new weekend deals" if is_weekend_mode else f"CocoWest: Monday Sales Post ({items_count} items)"
    send_email(subj, "\n".join(lines))


# ------------------------------------------------------------------- output

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1b8a3a">
<title>__PAGE_TITLE__ (__TOTAL__ items)</title>
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
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--fg); font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
         padding-bottom:3rem; padding-left:env(safe-area-inset-left,0px); padding-right:env(safe-area-inset-right,0px); }
  
  .sticky-header {
    position: sticky;
    top: 0;
    z-index: 10;
    background: var(--bar);
    -webkit-backdrop-filter: blur(8px);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--line);
    padding-top: env(safe-area-inset-top, 0px);
  }
  .bar { padding: .6rem .8rem .4rem; }
  .bar-row { max-width:1200px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; gap:.6rem; }
  .title { font-size:1.05rem; font-weight:700; }
  .badge-btn { background:var(--accent); color:var(--on-accent); border:none; border-radius:999px; padding:.35rem .85rem;
               font-size:.9rem; font-weight:700; cursor:pointer; white-space:nowrap; }
  .badge-btn.gray { background:#6c757d; color:#fff; }
  .badge-btn.active { background:var(--danger); color:#fff; }
  .search { max-width:1200px; margin:.5rem auto 0; display:flex; gap:.5rem; }
  .search input { flex:1; min-width:0; padding:.6rem .8rem; font-size:16px; border:1px solid var(--line); border-radius:10px;
                  background:var(--card); color:var(--fg); }
  .toolbar { max-width:1200px; margin:0 auto; padding:0 .8rem .6rem; display:flex; flex-wrap:wrap; align-items:center; gap:.35rem; }
  .chip { border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:999px; padding:.25rem .65rem;
          font-size:.8rem; font-weight:600; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; white-space:nowrap; }
  .chip.icon-only { padding:.25rem .5rem; font-size:.95rem; }
  .chip.active { background:var(--accent); color:var(--on-accent); border-color:var(--accent); }
  .chip.danger { color:var(--danger); border-color:var(--danger); }
  .cols-toggle { display:none; align-items:center; gap:.35rem; margin-left:auto; font-size:.8rem; color:var(--muted); }
  .seg { display:inline-flex; border:1px solid var(--line); border-radius:999px; overflow:hidden; }
  .seg button { border:none; background:var(--card); color:var(--fg); padding:.3rem .7rem; font-size:.85rem; font-weight:700; cursor:pointer; }
  .seg button.active { background:var(--accent); color:var(--on-accent); }
  
  .meta { max-width:1200px; margin:.6rem auto .4rem; padding:0 .8rem; color:var(--muted); font-size:.8rem; line-height:1.5; }
  .meta a { color:inherit; }
  
  .grid { max-width:1200px; margin:.6rem auto; padding:0 .8rem; display:grid; gap:.8rem; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); }
  .grid.c1 { grid-template-columns:minmax(0,1fr); }
  .grid.c2 { grid-template-columns:repeat(2,minmax(0,1fr)); }

  .card { margin:0; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; position:relative;
          display:flex; flex-direction:column; justify-content:space-between; }
  .card.is-new { border:2px solid var(--highlight-border); background:var(--highlight); }
  .card.is-expired img, .card.is-expired figcaption { opacity:.5; }
  .new-tag { position:absolute; top:8px; left:8px; background:#28a745; color:#fff; font-size:.7rem; font-weight:bold; padding:2px 6px;
             border-radius:4px; z-index:2; text-transform:uppercase; }
  .clr-tag { position:absolute; top:8px; right:8px; background:#6f42c1; color:#fff; font-size:.7rem; font-weight:bold; padding:2px 6px;
             border-radius:4px; z-index:2; text-transform:uppercase; }
  .card a { display:block; }
  .card img { width:100%; aspect-ratio:1/1; object-fit:contain; display:block; background:#fff; }
  .card figcaption { padding:.5rem .6rem; font-size:.85rem; line-height:1.3; flex-grow:1; overflow-wrap:anywhere; }
  .card-footer { padding:.4rem .6rem .6rem; display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:.3rem;
                 border-top:1px solid var(--line); font-size:.8rem; }
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
    .cols-toggle { display:flex; }
    .toolbar { padding:0 .6rem .5rem; gap:.25rem; }
    .chip { font-size:.72rem; padding:.22rem .5rem; }
    .chip.icon-only { font-size:.85rem; padding:.22rem .4rem; }
    .grid { padding:0 .5rem; gap:.5rem; }
    .grid.c1 { grid-template-columns:minmax(0,1fr); }
    .grid.c2 { grid-template-columns:repeat(2,minmax(0,1fr)); }
    .grid.c2 .card { border-radius:10px; }
    .grid.c2 .card figcaption { font-size:.72rem; padding:.4rem .45rem; }
    .grid.c2 .card-footer { padding:.35rem .45rem .5rem; font-size:.72rem; }
    .grid.c2 .new-tag, .grid.c2 .clr-tag { font-size:.6rem; top:5px; }
    .grid.c2 .new-tag { left:5px; }
    .grid.c2 .clr-tag { right:5px; }
    .grid.c2 .end-badge { font-size:.65rem; padding:2px 6px; }
    .title { font-size:.95rem; }
    .meta { padding:0 .6rem; }
  }
</style>
</head>
<body>
<div class="sticky-header">
  <header class="bar">
    <div class="bar-row">
      <div class="title">&#128717;&#65039; __PAGE_TITLE__</div>
      <button class="badge-btn" id="list-toggle" type="button">&#128722; List (0)</button>
    </div>
    <div class="search">
      <input id="q" type="search" placeholder="Filter by name or item number..." autocomplete="off">
      <button class="badge-btn gray" id="reset-btn" type="button">Reset</button>
    </div>
  </header>
  <div class="toolbar">
    __NEW_BTN__
    <button class="chip" id="clr-toggle" type="button" aria-pressed="false">&#127991;&#65039; Clearance (__CLR_COUNT__)</button>
    <button class="chip" id="week-toggle" type="button" aria-pressed="false">&#9200; Ends this week (0)</button>
    <button class="chip icon-only" data-cat="appliances" type="button" aria-pressed="false" title="Appliances">&#129368;</button>
    <button class="chip icon-only" data-cat="electronics" type="button" aria-pressed="false" title="Electronics">&#128187;</button>
    <button class="chip icon-only" data-cat="groceries" type="button" aria-pressed="false" title="Groceries">&#129382;</button>
    <button class="chip icon-only" data-cat="household" type="button" aria-pressed="false" title="Household">&#129532;</button>
    <button class="chip icon-only" data-cat="clothing" type="button" aria-pressed="false" title="Clothing">&#128084;</button>
    <button class="chip icon-only" data-cat="health" type="button" aria-pressed="false" title="Health">&#128138;</button>
    <button class="chip icon-only" data-cat="home" type="button" aria-pressed="false" title="Home">&#129692;</button>
    <button class="chip danger" id="clear-list" type="button" hidden>Clear list</button>
    <div class="cols-toggle">
      <span>Per row</span>
      <span class="seg">
        <button type="button" data-cols="1" aria-pressed="false">1</button>
        <button type="button" data-cols="2" aria-pressed="true">2</button>
      </span>
    </div>
  </div>
</div>
<div class="meta">
  📍 Store Location: <b>__LOCATION__</b><br>
  Showing <b id="shown-count">__TOTAL__</b> of <b>__TOTAL__</b> items &middot; updated __STAMP__<br>
  Source: <a href="__SOURCE_URL__">__SOURCE_NAME__</a>
</div>
<main class="grid c2" id="grid">
__CARDS__
<p class="empty" id="no-match" hidden></p>
</main>
<script>
(function () {
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
  var newBtn = document.getElementById('new-toggle');
  var clrBtn = document.getElementById('clr-toggle');
  var weekBtn = document.getElementById('week-toggle');
  var catBtns = Array.prototype.slice.call(document.querySelectorAll('[data-cat]'));
  var clearBtn = document.getElementById('clear-list');
  var resetBtn = document.getElementById('reset-btn');
  var shownEl = document.getElementById('shown-count');
  var emptyMsg = document.getElementById('no-match');
  var colBtns = Array.prototype.slice.call(document.querySelectorAll('[data-cols]'));
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var checks = Array.prototype.slice.call(document.querySelectorAll('.shop-check'));

  var shoppingList = load(LIST_KEY, []);
  if (!Array.isArray(shoppingList)) shoppingList = [];
  var state = { listOnly: false, newOnly: false, clrOnly: false, weekOnly: false, activeCat: '' };

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

  function countWhere(fn) {
    var n = 0;
    cards.forEach(function (el) { if (fn(el)) n++; });
    return n;
  }
  if (newBtn) {
    newBtn.innerHTML = '&#10024; New (' + countWhere(function (el) { return el.classList.contains('is-new'); }) + ')';
  }
  clrBtn.innerHTML = '&#127991;&#65039; Clearance (' + countWhere(function (el) { return el.classList.contains('is-clr'); }) + ')';
  weekBtn.innerHTML = '&#9200; Ends this week (' + countWhere(function (el) { return el.getAttribute('data-week') === '1'; }) + ')';

  function inList(url) { return shoppingList.indexOf(url) !== -1; }

  function listCountOnPage() {
    return countWhere(function (el) { return inList(el.getAttribute('data-url')); });
  }

  function render() {
    var t = q.value.trim().toLowerCase();
    var shown = 0;
    cards.forEach(function (el) {
      var ok = (!t || el.getAttribute('data-t').indexOf(t) !== -1) &&
               (!state.newOnly || el.classList.contains('is-new')) &&
               (!state.clrOnly || el.classList.contains('is-clr')) &&
               (!state.weekOnly || el.getAttribute('data-week') === '1') &&
               (!state.activeCat || el.getAttribute('data-cat') === state.activeCat) &&
               (!state.listOnly || inList(el.getAttribute('data-url')));
      el.hidden = !ok;
      if (ok) shown++;
    });

    var n = listCountOnPage();
    listBtn.innerHTML = state.listOnly ? 'Show all items' : '&#128722; List (' + n + ')';
    listBtn.classList.toggle('active', state.listOnly);
    
    var toggles = [clrBtn, weekBtn];
    if (newBtn) toggles.push(newBtn);
    toggles.forEach(function (btn) {
      if (!btn) return;
      var active = (btn === newBtn && state.newOnly) || (btn === clrBtn && state.clrOnly) || (btn === weekBtn && state.weekOnly);
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });

    catBtns.forEach(function (btn) {
      var cat = btn.getAttribute('data-cat');
      var active = (state.activeCat === cat);
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });

    clearBtn.hidden = n === 0;
    clearBtn.textContent = 'Clear list (' + n + ')';
    shownEl.textContent = shown;

    if (shown === 0) {
      emptyMsg.textContent = (state.listOnly && n === 0)
        ? 'Your list is empty. Tick "Add to list" on any item.'
        : 'No items match your filters.';
    }
    emptyMsg.hidden = shown !== 0;
  }

  checks.forEach(function (chk) {
    var url = chk.getAttribute('data-url');
    chk.checked = inList(url);
    chk.addEventListener('change', function () {
      if (chk.checked) {
        if (!inList(url)) shoppingList.push(url);
      } else {
        shoppingList = shoppingList.filter(function (u) { return u !== url; });
      }
      save(LIST_KEY, shoppingList);
      render();
    });
  });

  listBtn.addEventListener('click', function () { state.listOnly = !state.listOnly; render(); });
  if (newBtn) {
    newBtn.addEventListener('click', function () { state.newOnly = !state.newOnly; render(); });
  }
  clrBtn.addEventListener('click', function () { state.clrOnly = !state.clrOnly; render(); });
  weekBtn.addEventListener('click', function () { state.weekOnly = !state.weekOnly; render(); });
  
  catBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var cat = btn.getAttribute('data-cat');
      state.activeCat = (state.activeCat === cat) ? '' : cat;
      render();
    });
  });

  q.addEventListener('input', render);

  resetBtn.addEventListener('click', function () {
    q.value = '';
    state.newOnly = false;
    state.clrOnly = false;
    state.weekOnly = false;
    state.listOnly = false;
    state.activeCat = '';
    render();
  });

  clearBtn.addEventListener('click', function () {
    var n = listCountOnPage();
    if (!n) return;
    if (!window.confirm('Remove all ' + n + ' items from your shopping list?')) return;
    shoppingList = [];
    save(LIST_KEY, shoppingList);
    checks.forEach(function (chk) { chk.checked = false; });
    state.listOnly = false;
    state.activeCat = '';
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
})();
</script>
</body>
</html>"""

def render_html(items, monday_url, weekend_url, store_location, is_weekend_mode, exclusive_urls=None) -> str:
    stamp = datetime.now(ZoneInfo("America/Vancouver")).strftime("%b %d, %I:%M %p")
    esc = html.escape
    exclusive_urls = exclusive_urls or set()
    
    clr_count = sum(1 for i in items if is_clearance(i.label))
    new_count = len(exclusive_urls)

    if is_weekend_mode:
        page_title = "Costco Weekend Deals"
        new_btn_html = f'<button class="chip" id="new-toggle" type="button" aria-pressed="false">&#10024; New ({new_count})</button>'
        source_url = weekend_url
        source_name = "Weekend Update"
    else:
        page_title = "Costco Monday Sales Post"
        new_btn_html = ''
        source_url = monday_url
        source_name = "Monday Sales Post"

    cards = []
    for i in items:
        is_new = i.url in exclusive_urls
        clearance = is_clearance(i.label)
        end = parse_end_date(i.label)

        classes = ["card"]
        if is_new:
            classes.append("is-new")
        if clearance:
            classes.append("is-clr")

        attrs = f'data-t="{esc(i.label.lower(), quote=True)}" data-url="{esc(i.url, quote=True)}" data-cat="{esc(i.category, quote=True)}"'
        if end:
            attrs += f' data-end="{end}"'

        new_tag = '<div class="new-tag">&#10024; New</div>' if is_new else ''
        clr_tag = '<div class="clr-tag">&#127991;&#65039; Clearance</div>' if clearance else ''
        end_badge = '<span class="end-badge" hidden></span>' if end else ''

        cards.append(f'''<figure class="{' '.join(classes)}" {attrs}>
  {new_tag}{clr_tag}
  <a href="{esc(i.url, quote=True)}" target="_blank" rel="noopener">
    <img src="{esc(i.url, quote=True)}" alt="{esc(i.label, quote=True)}" loading="lazy" referrerpolicy="no-referrer">
  </a>
  <figcaption>{esc(i.label)}</figcaption>
  <div class="card-footer">
    <label class="shop-label">
      <input type="checkbox" class="shop-check" data-url="{esc(i.url, quote=True)}"> Add to list
    </label>
    {end_badge}
  </div>
</figure>''')

    cards_html = "\n".join(cards) or '<p class="empty">No items found.</p>'

    return (PAGE_TEMPLATE
            .replace("__PAGE_TITLE__", page_title)
            .replace("__TOTAL__", str(len(items)))
            .replace("__CLR_COUNT__", str(clr_count))
            .replace("__NEW_BTN__", new_btn_html)
            .replace("__LOCATION__", esc(store_location or "Metro Vancouver Area"))
            .replace("__STAMP__", stamp)
            .replace("__SOURCE_URL__", esc(source_url, quote=True))
            .replace("__SOURCE_NAME__", source_name)
            .replace("__CARDS__", cards_html))

def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    scraper = make_scraper()

    monday_url, weekend_url = os.getenv("MONDAY_URL"), os.getenv("WEEKEND_URL")
    if not monday_url:
        home = fetch(scraper, BASE_URL)
        monday_url, weekend_url = discover_posts(home)

    if not monday_url:
        print("ERROR: could not find Monday/Sales post.", file=sys.stderr)
        return 1

    print(f"Base Flyer/Sales Post: {monday_url}")
    monday_html = fetch(scraper, monday_url)
    monday_items = extract_items(monday_html, monday_url)

    if not monday_items:
        print("ERROR: zero images extracted from base sales post; refusing to deploy.", file=sys.stderr)
        return 1

    store_location = extract_location(monday_html)

    is_weekend_mode = bool(weekend_url)
    weekend_items = []
    exclusive = []
    exclusive_urls = set()

    if is_weekend_mode:
        print(f"Weekend Update: {weekend_url}")
        time.sleep(random.uniform(1, 3))
        weekend_html = fetch(scraper, weekend_url)
        weekend_items = extract_items(weekend_html, weekend_url)
        
        if weekend_items:
            store_location = extract_location(weekend_html) or store_location
            exclusive = find_exclusive_userscript(weekend_items, monday_items) if MATCH_MODE == "userscript" else find_exclusive(weekend_items, monday_items)
            exclusive_urls = {i.url for i in exclusive}
        else:
            is_weekend_mode = False

    target_items = weekend_items if is_weekend_mode else monday_items
    target_url = weekend_url if is_weekend_mode else monday_url

    print(f"Mode: {'Weekend Exclusives' if is_weekend_mode else 'Monday Sales Post'}")
    print(f"Location: {store_location}")
    print(f"Total items: {len(target_items)} | New exclusives: {len(exclusive_urls)}")

    prev_state, state_ok = load_previous_state(scraper)

    (OUTPUT_DIR / "index.html").write_text(
        render_html(target_items, monday_url, weekend_url or monday_url, store_location, is_weekend_mode, exclusive_urls),
        encoding="utf-8")
    
    (OUTPUT_DIR / "state.json").write_text(json.dumps({
        "weekend_url": weekend_url,
        "flyer_url": monday_url,
        "mode": "weekend" if is_weekend_mode else "flyer",
        "total_count": len(target_items),
        "exclusive_count": len(exclusive),
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")

    maybe_notify(prev_state, state_ok, target_url, is_weekend_mode, len(target_items), len(exclusive))
    return 0

if __name__ == "__main__":
    sys.exit(main())
