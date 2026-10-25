#!/usr/bin/env python3
"""
Scrape CocoWest.ca for the weekly Costco flyer post vs the Weekend Update post,
isolate exclusive weekend items, and render a static, mobile-friendly index.html.

Env overrides (handy for debugging in Actions):
  MONDAY_URL, WEEKEND_URL     skip homepage discovery
  MONDAY_RE, WEEKEND_RE       regexes used to classify homepage links
  SKIP_DATE_CHECK=1           don't require matching date ranges in the two URLs
  MATCH_MODE                  "smart" (default) or "userscript" (alt text / src filename only)
  OUTPUT_DIR                  default: public
  DEBUG=1                     dump fetched HTML + JSON next to index.html
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

# CocoWest titles: "Costco Flyer & Costco Sale Items for October 19-25, 2026 ..."
#                  "Weekend Update! - Costco Sale Items for October 23-25, 2026 ..."
MONDAY_RE = re.compile(os.getenv("MONDAY_RE", r"flyer"), re.I)
WEEKEND_RE = re.compile(os.getenv("WEEKEND_RE", r"weekend"), re.I)

FUZZY_THRESHOLD = 0.92
STRIP_SIZE_SUFFIX = False  # set True to turn foo-300x200.jpg into foo.jpg

# ----------------------------------------------------------------- fetching

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
        except Exception as exc:  # network / cloudscraper errors
            last = repr(exc)
        wait = 2 ** attempt + random.random() * 2
        print(f"[fetch] {url} attempt {attempt}/{retries} failed ({last}); sleeping {wait:.1f}s",
              file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"Could not fetch {url}: {last}")


# ---------------------------------------------------------------- discovery

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
    if not (monday and weekend):
        print("[discover] candidate links found on homepage:", file=sys.stderr)
        for href, blob in candidates[:25]:
            print(f"   {href}  |  {blob}", file=sys.stderr)
    return monday, weekend


def end_day(url: str):
    """'...october-19-25-2026...' -> '25'. Used to check both posts cover the same week."""
    m = re.search(r"(\d{1,2})-(\d{1,2})-20\d\d", url)
    return m.group(2) if m else None


# ---------------------------------------------------------- image extraction

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

# Costco item numbers: CocoWest alt text looks like "1218587 SWIFFER DUSTERS REFILLS ... $19.99"
ALT_ITEM_NO_RE = re.compile(r"^\s*(\d{5,8})\b")
FILE_ITEM_NO_RE = re.compile(r"^(\d{5,8})[-_]")


@dataclass
class Item:
    url: str
    label: str
    alt: str
    alt_key: str
    file_key: str
    us_key: str    # the userscript's identity rule: raw alt, else filename of <img src>
    item_no: str   # Costco item number if one is found in the alt text / filename, else ""


def in_bad_region(el, container) -> bool:
    """Check only the ancestors between the image and the container (exclusive),
    and never <body>/<html> (their classes like 'has-sidebar' say nothing about the image)."""
    for parent in el.parents:
        if parent is container:
            break
        name = getattr(parent, "name", None)
        if name in (None, "[document]", "html", "body"):
            continue
        if name in ("nav", "footer", "aside"):
            return True
        # page-level header only: ignore <header> inside <article> (post title/featured image)
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
    first = value.split(",")[0].strip().split(" ")[0]
    return first or None


def usable(url) -> bool:
    return bool(url) and not url.startswith("data:") and not re.search(
        r"(placeholder|blank\.gif|1x1|lazy[-_]?load|spinner)", url, re.I)


def candidate_urls(img):
    """All possible image URLs for this <img>, best first."""
    out = []
    parent = img.find_parent("a", href=True)
    if parent and IMG_URL_RE.match(parent["href"]):
        out.append(parent["href"])                       # full-size link

    for attr in SRCSET_ATTRS:
        if img.get(attr):
            out.append(largest_from_srcset(img[attr]))
    for attr in KNOWN_LAZY_ATTRS:
        out.append(img.get(attr))
    # unknown custom attrs: any data-* value that looks like an image URL
    for attr, val in img.attrs.items():
        if attr.startswith("data-") and isinstance(val, str) and IMG_URL_RE.match(val.strip()):
            out.append(val.strip())
    out.append(img.get("src"))
    return [u.strip() for u in out if isinstance(u, str) and usable(u.strip())]


def strip_size_suffix(url: str) -> str:
    return re.sub(r"-\d{2,4}x\d{2,4}(?=\.[a-z]{3,4}(\?|$))", "", url, flags=re.I)


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def filename_key(url: str):
    name = Path(unquote(urlparse(url).path)).stem.lower()
    prev = None
    while prev != name:
        prev = name
        name = re.sub(r"-\d{2,4}x\d{2,4}$", "", name)
        name = re.sub(r"-(scaled|rotated|edited|copy|min|large|medium|thumbnail)$", "", name)
        name = re.sub(r"[-_]\d{1,2}$", "", name)         # WP duplicate-upload suffix
    key = re.sub(r"[^a-z0-9]", "", name)
    if len(key) < 5 or GENERIC_NAME_RE.match(key):
        return ""
    return key


def pretty_name(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).stem
    name = re.sub(r"-\d{2,4}x\d{2,4}$", "", name)
    return re.sub(r"[-_]+", " ", name).strip().title()


def userscript_key(img) -> str:
    """Same rule as the Tampermonkey script: img.alt || last path segment of img.src."""
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
    """Return None if the image should be kept, else a short reason string."""
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
    # <noscript> fallbacks: html.parser exposes children directly (already covered above),
    # but with other parsers the content is a string, so handle that case too.
    for ns in container.find_all("noscript"):
        if ns.string:
            imgs.extend(BeautifulSoup(ns.string, "html.parser").find_all("img"))
    stats["img_tags_in_container"] = len(imgs)

    items, seen = [], set()
    for img in imgs:
        cands = candidate_urls(img)
        if not cands:
            stats["rejected:no_usable_url"] += 1
            continue
        url = urljoin(page_url, cands[0])
        if STRIP_SIZE_SUFFIX:
            url = strip_size_suffix(url)
        reason = reject_reason(img, url, container)
        if reason:
            stats[f"rejected:{reason}"] += 1
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
            stats["rejected:duplicate"] += 1
            continue
        seen.add(dedupe_key)

        items.append(Item(url=url, label=alt if good_alt else pretty_name(url),
                          alt=alt, alt_key=alt_key, file_key=file_key,
                          us_key=userscript_key(img), item_no=item_no))
    stats["kept_with_item_no"] = sum(1 for i in items if i.item_no)
    return items, imgs


def extract_items(post_html: str, page_url: str, name: str = ""):
    soup = BeautifulSoup(post_html, "html.parser")

    # Try each plausible post-body container, then the whole document, until one yields items.
    containers = []
    for sel in CONTAINER_SELECTORS:
        el = soup.select_one(sel)
        if el is not None and el.find("img"):
            containers.append((sel, el))
    containers.append(("(whole document)", soup))

    items, imgs, stats, used = [], [], Counter(), ""
    for used, el in containers:
        stats = Counter()
        items, imgs = extract_from(el, page_url, stats)
        if items:
            break

    # ---- diagnostics ----
    print(f"[{name}] html_bytes={len(post_html)} total_img_tags_on_page={len(soup.find_all('img'))} "
          f"container={used!r} kept={len(items)}")
    for k, v in sorted(stats.items()):
        print(f"[{name}]   {k}: {v}")
    if not items:
        print(f"[{name}] no images kept. First raw <img> tags on the page:")
        for img in soup.find_all("img")[:4]:
            print("    ", str(img)[:350].replace("\n", " "))
    return items


# --------------------------------------------------------------- comparison

def find_exclusive(weekend_items, monday_items):
    """Smart matching.
    Items with a Costco item number: duplicate if that number (or the exact cleaned filename)
    appears in the flyer post. Items without one: normalized alt, filename, then fuzzy alt."""
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
    """Mirror of the Tampermonkey rule: exact alt (or src filename) not seen on Monday."""
    mon = {i.us_key for i in monday_items if i.us_key}
    return [i for i in weekend_items if i.us_key and i.us_key not in mon]


def print_samples(tag: str, group, n: int = 12):
    print(f"--- {tag} sample ({len(group)} total) ---")
    step = max(1, len(group) // n)
    for it in group[::step][:n]:
        print(f"  no={it.item_no or '-':<8} alt={it.alt[:60]!r}  file={Path(urlparse(it.url).path).name}")


# ------------------------------------------------------------------- output

# Plain string with __TOKENS__ (not an f-string) so CSS/JS braces need no escaping.
PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1b8a3a">
<title>New CocoWest Weekend Deals (__COUNT__)</title>
<style>
  :root {
    --bg:#f4f5f7; --fg:#1a1a1a; --card:#ffffff; --muted:#666; --line:#e2e2e2;
    --accent:#1b8a3a; --bar:rgba(255,255,255,.92);
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#121212; --fg:#eeeeee; --card:#1e1e1e; --muted:#9a9a9a; --line:#333;
            --accent:#3ddc6a; --bar:rgba(18,18,18,.92); }
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
         padding-left:env(safe-area-inset-left,0px); padding-right:env(safe-area-inset-right,0px);
         padding-bottom:env(safe-area-inset-bottom,0px); }
  .bar { position:sticky; top:0; z-index:10; background:var(--bar);
         -webkit-backdrop-filter:blur(8px); backdrop-filter:blur(8px);
         border-bottom:1px solid var(--line);
         padding:calc(.6rem + env(safe-area-inset-top,0px)) .8rem .6rem; }
  .bar-row { max-width:1200px; margin:0 auto; display:flex; align-items:center;
             justify-content:space-between; gap:.6rem; }
  .title { font-size:1.05rem; font-weight:700; line-height:1.2; }
  .count { background:var(--accent); color:#fff; border-radius:999px; padding:.3rem .8rem;
           font-size:.95rem; font-weight:700; white-space:nowrap; }
  .count b { font-size:1.15rem; }
  @media (prefers-color-scheme: dark) { .count { color:#08210f; } }
  .search { max-width:1200px; margin:.5rem auto 0; }
  .search input { width:100%; padding:.65rem .8rem; font-size:16px; /* 16px stops iOS zoom */
                  border:1px solid var(--line); border-radius:10px;
                  background:var(--card); color:var(--fg); }
  .meta { max-width:1200px; margin:.7rem auto .2rem; padding:0 .8rem; color:var(--muted);
          font-size:.8rem; line-height:1.5; }
  .meta a { color:inherit; }
  .grid { max-width:1200px; margin:.6rem auto 2rem; padding:0 .8rem; display:grid; gap:.8rem;
          grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); }
  .card { margin:0; background:var(--card); border:1px solid var(--line);
          border-radius:12px; overflow:hidden; }
  .card[hidden] { display:none; }
  .card a { display:block; }
  .card img { width:100%; aspect-ratio:1/1; object-fit:contain; display:block; background:#fff; }
  .card figcaption { padding:.5rem .6rem .7rem; font-size:.85rem; line-height:1.3;
                     overflow-wrap:anywhere; }
  .empty { padding:2rem 1rem; text-align:center; color:var(--muted); grid-column:1/-1; }
  @media (max-width:640px) {
    .grid { grid-template-columns:repeat(2,minmax(0,1fr)); gap:.5rem; padding:0 .5rem; }
    .card { border-radius:10px; }
    .card figcaption { font-size:.72rem; padding:.4rem .45rem .55rem; }
    .title { font-size:.95rem; }
    .meta { padding:0 .6rem; }
  }
</style>
</head>
<body>
<header class="bar">
  <div class="bar-row">
    <div class="title">&#127381; New weekend deals</div>
    <div class="count"><b id="shown">__COUNT__</b> new</div>
  </div>
  <div class="search">
    <input id="q" type="search" placeholder="Filter by name or item number" autocomplete="off">
  </div>
</header>
<div class="meta">
  __WEEKEND_N__ weekend items &minus; __FLYER_N__ flyer items &middot; updated __STAMP__<br>
  Sources: <a href="__FLYER_URL__">Flyer post</a> &middot; <a href="__WEEKEND_URL__">Weekend post</a>
</div>
<main class="grid" id="grid">
__CARDS__
</main>
<script>
(function () {
  var q = document.getElementById('q');
  var shown = document.getElementById('shown');
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var total = cards.length;
  q.addEventListener('input', function () {
    var t = q.value.trim().toLowerCase();
    var n = 0;
    cards.forEach(function (el) {
      var match = !t || el.getAttribute('data-t').indexOf(t) !== -1;
      el.hidden = !match;
      if (match) n++;
    });
    shown.textContent = (n === total) ? String(total) : (n + ' of ' + total);
  });
})();
</script>
</body>
</html>"""


def render_html(items, monday_url, weekend_url, n_monday, n_weekend) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    esc = html.escape
    cards = "\n".join(
        f'''<figure class="card" data-t="{esc(i.label.lower(), quote=True)}">
  <a href="{esc(i.url, quote=True)}" target="_blank" rel="noopener">
    <img src="{esc(i.url, quote=True)}" alt="{esc(i.label, quote=True)}" loading="lazy" referrerpolicy="no-referrer">
  </a>
  <figcaption>{esc(i.label)}</figcaption>
</figure>''' for i in items
    ) or '<p class="empty">No exclusive weekend items found.</p>'

    return (PAGE_TEMPLATE
            .replace("__COUNT__", str(len(items)))
            .replace("__WEEKEND_N__", str(n_weekend))
            .replace("__FLYER_N__", str(n_monday))
            .replace("__STAMP__", stamp)
            .replace("__FLYER_URL__", esc(monday_url, quote=True))
            .replace("__WEEKEND_URL__", esc(weekend_url, quote=True))
            .replace("__CARDS__", cards))


# --------------------------------------------------------------------- main

def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    scraper = make_scraper()

    monday_url, weekend_url = os.getenv("MONDAY_URL"), os.getenv("WEEKEND_URL")
    if not (monday_url and weekend_url):
        home = fetch(scraper, BASE_URL)
        if DEBUG:
            (OUTPUT_DIR / "debug_home.html").write_text(home, encoding="utf-8")
        monday_url, weekend_url = discover_posts(home)
    if not (monday_url and weekend_url):
        print(f"ERROR: could not find both posts (monday={monday_url}, weekend={weekend_url}). "
              "Adjust MONDAY_RE / WEEKEND_RE or set MONDAY_URL / WEEKEND_URL.", file=sys.stderr)
        return 1
    print(f"Flyer  : {monday_url}\nWeekend: {weekend_url}")

    # Both posts should cover the same week (e.g. 19-25 and 23-25 both end on the 25th).
    if os.getenv("SKIP_DATE_CHECK") != "1":
        a, b = end_day(monday_url), end_day(weekend_url)
        if a and b and a != b:
            print("WARNING: date ranges don't line up (this week's weekend post may not be "
                  "up yet). Exiting without deploying.", file=sys.stderr)
            return 1

    monday_html = fetch(scraper, monday_url)
    time.sleep(random.uniform(1, 3))
    weekend_html = fetch(scraper, weekend_url)
    if DEBUG:
        (OUTPUT_DIR / "debug_monday.html").write_text(monday_html, encoding="utf-8")
        (OUTPUT_DIR / "debug_weekend.html").write_text(weekend_html, encoding="utf-8")

    monday_items = extract_items(monday_html, monday_url, "flyer")
    weekend_items = extract_items(weekend_html, weekend_url, "weekend")
    print(f"Extracted {len(monday_items)} flyer and {len(weekend_items)} weekend images")

    # Safety: an empty result usually means a scrape problem, not "no deals".
    if not monday_items or not weekend_items:
        print("ERROR: one of the posts yielded zero images; refusing to deploy.", file=sys.stderr)
        return 1

    smart = find_exclusive(weekend_items, monday_items)
    us = find_exclusive_userscript(weekend_items, monday_items)
    print(f"Exclusive items -> smart rules: {len(smart)} | userscript-style rule: {len(us)} "
          f"| MATCH_MODE={MATCH_MODE}")
    exclusive = us if MATCH_MODE == "userscript" else smart

    print_samples("flyer", monday_items)
    print_samples("weekend", weekend_items)
    print_samples("EXCLUSIVE (used for page)", exclusive)

    (OUTPUT_DIR / "index.html").write_text(
        render_html(exclusive, monday_url, weekend_url, len(monday_items), len(weekend_items)),
        encoding="utf-8")
    if DEBUG:
        (OUTPUT_DIR / "debug_items.json").write_text(json.dumps(
            {"monday": [asdict(i) for i in monday_items],
             "weekend": [asdict(i) for i in weekend_items],
             "exclusive_smart": [asdict(i) for i in smart],
             "exclusive_userscript": [asdict(i) for i in us]}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
