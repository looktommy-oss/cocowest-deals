#!/usr/bin/env python3
"""
Scrape CocoWest.ca for the weekly Costco flyer post vs the Weekend Update post,
display ALL weekend items with exclusive ones highlighted, and include an interactive shopping list.

Env overrides:
  MONDAY_URL, WEEKEND_URL      skip homepage discovery
  MATCH_MODE                   "smart" (default) or "userscript"
  OUTPUT_DIR                   default: public
  DEBUG=1                      dump fetched HTML + JSON
"""
import html
import json
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup

BASE_URL = "https://www.cocowest.ca/"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "public"))
DEBUG = os.getenv("DEBUG") == "1"
MATCH_MODE = os.getenv("MATCH_MODE", "smart").lower()

MONDAY_RE = re.compile(os.getenv("MONDAY_RE", r"flyer"), re.I)
WEEKEND_RE = re.compile(os.getenv("WEEKEND_RE", r"weekend"), re.I)

FUZZY_THRESHOLD = 0.92
STRIP_SIZE_SUFFIX = False

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

def end_day(url: str):
    m = re.search(r"(\d{1,2})-(\d{1,2})-20\d\d", url)
    return m.group(2) if m else None

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
    r"advert|sponsor|cropped-|blank|pixel|1x1|spacer|newsletter|subscribe)", re.I)
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

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1b8a3a">
<title>CocoWest Weekend Deals (__TOTAL__ items)</title>
<style>
  :root {
    --bg:#f4f5f7; --fg:#1a1a1a; --card:#ffffff; --muted:#666; --line:#e2e2e2;
    --accent:#1b8a3a; --bar:rgba(255,255,255,.94); --highlight:#e6f4ea; --highlight-border:#34a853;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#121212; --fg:#eeeeee; --card:#1e1e1e; --muted:#9a9a9a; --line:#333;
            --accent:#3ddc6a; --bar:rgba(18,18,18,.94); --highlight:#132618; --highlight-border:#28a745; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font-family:system-ui,-apple-system,sans-serif; padding-bottom:3rem; }
  .bar { position:sticky; top:0; z-index:10; background:var(--bar); backdrop-filter:blur(8px); border-bottom:1px solid var(--line); padding:.6rem .8rem; }
  .bar-row { max-width:1200px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; gap:.6rem; }
  .title { font-size:1.05rem; font-weight:700; }
  .actions { display:flex; gap:.5rem; align-items:center; }
  .badge-btn { background:var(--accent); color:#fff; border:none; border-radius:999px; padding:.3rem .8rem; font-size:.9rem; font-weight:700; cursor:pointer; }
  .search { max-width:1200px; margin:.5rem auto 0; display:flex; gap:.5rem; }
  .search input { flex:1; padding:.6rem .8rem; font-size:16px; border:1px solid var(--line); border-radius:10px; background:var(--card); color:var(--fg); }
  .meta { max-width:1200px; margin:.6rem auto .2rem; padding:0 .8rem; color:var(--muted); font-size:.8rem; line-height:1.4; }
  .meta a { color:inherit; }
  .grid { max-width:1200px; margin:.6rem auto; padding:0 .8rem; display:grid; gap:.8rem; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); }
  .card { margin:0; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; position:relative; display:flex; flex-direction:column; justify-content:space-between; }
  .card.is-new { border:2px solid var(--highlight-border); background:var(--highlight); }
  .card[hidden] { display:none; }
  .new-tag { position:absolute; top:8px; left:8px; background:#28a745; color:#fff; font-size:.7rem; font-weight:bold; padding:2px 6px; border-radius:4px; z-index:2; text-transform:uppercase; }
  .card img { width:100%; aspect-ratio:1/1; object-fit:contain; display:block; background:#fff; }
  .card figcaption { padding:.5rem .6rem; font-size:.85rem; line-height:1.3; flex-grow:1; }
  .card-footer { padding:.4rem .6rem .6rem; display:flex; align-items:center; justify-content:space-between; border-top:1px solid var(--line); font-size:.8rem; }
  .shop-label { display:flex; align-items:center; gap:.4rem; cursor:pointer; font-weight:500; }
  .shop-label input { width:1.1rem; height:1.1rem; accent-color:var(--accent); cursor:pointer; }
  .empty { padding:2rem; text-align:center; color:var(--muted); grid-column:1/-1; }
</style>
</head>
<body>
<header class="bar">
  <div class="bar-row">
    <div class="title">&#128717;&#65039; Costco Weekend Deals</div>
    <div class="actions">
      <button class="badge-btn" id="list-toggle" onclick="toggleShoppingList()">🛒 List (<span id="shop-count">0</span>)</button>
    </div>
  </div>
  <div class="search">
    <input id="q" type="search" placeholder="Filter by name or item number..." autocomplete="off">
    <button class="badge-btn" style="background:#6c757d;" onclick="clearFilter()">Reset</button>
  </div>
</header>
<div class="meta">
  Showing all <b>__TOTAL__</b> weekend items (<b>__NEW_COUNT__</b> new exclusives highlighted) &middot; updated __STAMP__<br>
  Sources: <a href="__FLYER_URL__">Monday Flyer</a> &middot; <a href="__WEEKEND_URL__">Weekend Update</a>
</div>
<main class="grid" id="grid">
__CARDS__
</main>
<script>
var shoppingList = JSON.parse(localStorage.getItem('cocowest_shopping_list') || '[]');
var showingOnlyList = false;

function updateShopCount() {
  document.getElementById('shop-count').textContent = shoppingList.length;
}

function saveList() {
  localStorage.setItem('cocowest_shopping_list', JSON.stringify(shoppingList));
  updateShopCount();
}

function toggleItem(url, checked) {
  if (checked) {
    if (!shoppingList.includes(url)) shoppingList.push(url);
  } else {
    shoppingList = shoppingList.filter(u => u !== url);
  }
  saveList();
}

function toggleShoppingList() {
  showingOnlyList = !showingOnlyList;
  var btn = document.getElementById('list-toggle');
  btn.style.background = showingOnlyList ? '#dc3545' : '';
  btn.textContent = showingOnlyList ? 'Show All Items' : '🛒 List (' + shoppingList.length + ')';
  filterCards();
}

function clearFilter() {
  document.getElementById('q').value = '';
  showingOnlyList = false;
  document.getElementById('list-toggle').textContent = '🛒 List (' + shoppingList.length + ')';
  filterCards();
}

function filterCards() {
  var t = document.getElementById('q').value.trim().toLowerCase();
  var cards = document.querySelectorAll('.card');
  cards.forEach(function(el) {
    var url = el.getAttribute('data-url');
    var matchesSearch = !t || el.getAttribute('data-t').indexOf(t) !== -1;
    var matchesList = !showingOnlyList || shoppingList.includes(url);
    el.hidden = !(matchesSearch && matchesList);
  });
}

document.getElementById('q').addEventListener('input', filterCards);

// Initialize checkbox states from storage on load
document.addEventListener('DOMContentLoaded', function() {
  updateShopCount();
  document.querySelectorAll('.shop-check').forEach(function(chk) {
    var url = chk.getAttribute('data-url');
    if (shoppingList.includes(url)) {
      chk.checked = true;
    }
    chk.addEventListener('change', function() {
      toggleItem(url, chk.checked);
    });
  });
});
</script>
</body>
</html>"""

def render_html(weekend_items, exclusive_urls, monday_url, weekend_url, n_monday) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    esc = html.escape
    new_count = len(exclusive_urls)
    
    cards = []
    for i in weekend_items:
        is_new = i.url in exclusive_urls
        card_class = "card is-new" if is_new else "card"
        badge = '<div class="new-tag">✨ New</div>' if is_new else ''
        label_lower = esc(i.label.lower(), quote=True)
        
        cards.append(f'''<figure class="{card_class}" data-t="{label_lower}" data-url="{esc(i.url, quote=True)}">
  {badge}
  <a href="{esc(i.url, quote=True)}" target="_blank" rel="noopener">
    <img src="{esc(i.url, quote=True)}" alt="{esc(i.label, quote=True)}" loading="lazy" referrerpolicy="no-referrer">
  </a>
  <figcaption>{esc(i.label)}</figcaption>
  <div class="card-footer">
    <label class="shop-label">
      <input type="checkbox" class="shop-check" data-url="{esc(i.url, quote=True)}"> Add to list
    </label>
  </div>
</figure>''')
    
    cards_html = "\n".join(cards) or '<p class="empty">No weekend items found.</p>'

    return (PAGE_TEMPLATE
            .replace("__TOTAL__", str(len(weekend_items)))
            .replace("__NEW_COUNT__", str(new_count))
            .replace("__STAMP__", stamp)
            .replace("__FLYER_URL__", esc(monday_url, quote=True))
            .replace("__WEEKEND_URL__", esc(weekend_url, quote=True))
            .replace("__CARDS__", cards_html))

def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    scraper = make_scraper()

    monday_url, weekend_url = os.getenv("Monday_URL") or os.getenv("MONDAY_URL"), os.getenv("WEEKEND_URL")
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

    exclusive = find_exclusive_userscript(weekend_items, monday_items) if MATCH_MODE == "userscript" else find_exclusive(weekend_items, monday_items)
    exclusive_urls = {i.url for i in exclusive}

    print(f"Total weekend items: {len(weekend_items)} | New exclusives highlighted: {len(exclusive_urls)}")

    (OUTPUT_DIR / "index.html").write_text(
        render_html(weekend_items, exclusive_urls, monday_url, weekend_url, len(monday_items)),
        encoding="utf-8")
    return 0

if __name__ == "__main__":
    sys.exit(main())
