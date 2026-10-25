#!/usr/bin/env python3
"""
Scrape CocoWest.ca for the weekly Costco flyer post vs the Weekend Update post,
isolate exclusive weekend items, and render a static index.html.

Env overrides (handy for debugging in Actions):
  MONDAY_URL, WEEKEND_URL     skip homepage discovery
  MONDAY_RE, WEEKEND_RE       regexes used to classify homepage links
  SKIP_DATE_CHECK=1           don't require matching date ranges in the two URLs
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


@dataclass
class Item:
    url: str
    label: str
    alt: str
    alt_key: str
    file_key: str


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
        dedupe_key = file_key or alt_key or url
        if dedupe_key in seen:
            stats["rejected:duplicate"] += 1
            continue
        seen.add(dedupe_key)

        items.append(Item(url=url, label=alt if good_alt else pretty_name(url),
                          alt=alt, alt_key=alt_key, file_key=file_key))
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
    mon_files = {i.file_key for i in monday_items if i.file_key}
    mon_alts = [i.alt_key for i in monday_items if i.alt_key]
    mon_alt_set = set(mon_alts)

    def duplicate(it: Item) -> bool:
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


# ------------------------------------------------------------------- output

def render_html(items, monday_url, weekend_url, n_monday, n_weekend) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    esc = html.escape
    cards = "\n".join(
        f'''<figure class="card">
  <a href="{esc(i.url, quote=True)}" target="_blank" rel="noopener">
    <img src="{esc(i.url, quote=True)}" alt="{esc(i.label, quote=True)}" loading="lazy" referrerpolicy="no-referrer">
  </a>
  <figcaption>{esc(i.label)}</figcaption>
</figure>''' for i in items
    ) or "<p>No exclusive weekend items found.</p>"

    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Costco Weekend Exclusives</title>
<style>
  :root {{ --bg:#fafafa; --fg:#1a1a1a; --card:#fff; --muted:#666; --line:#e2e2e2; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#141414; --fg:#eee; --card:#1f1f1f; --muted:#999; --line:#333; }}
  }}
  body {{ margin:0; padding:1rem; background:var(--bg); color:var(--fg);
         font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  header {{ max-width:1200px; margin:0 auto 1rem; }}
  h1 {{ margin:.2rem 0; font-size:1.5rem; }}
  .meta {{ color:var(--muted); font-size:.85rem; }}
  .meta a {{ color:inherit; }}
  .grid {{ max-width:1200px; margin:0 auto; display:grid; gap:.8rem;
          grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); }}
  .card {{ margin:0; background:var(--card); border:1px solid var(--line);
          border-radius:10px; overflow:hidden; }}
  .card img {{ width:100%; aspect-ratio:1/1; object-fit:contain; display:block; background:#fff; }}
  .card figcaption {{ padding:.5rem .6rem; font-size:.85rem; line-height:1.3; }}
</style>
</head>
<body>
<header>
  <h1>Weekend-exclusive deals ({len(items)})</h1>
  <div class="meta">
    {n_weekend} weekend items &minus; {n_monday} flyer items &middot; updated {stamp}<br>
    Sources: <a href="{esc(monday_url, quote=True)}">Flyer post</a> &middot;
    <a href="{esc(weekend_url, quote=True)}">Weekend post</a>
  </div>
</header>
<main class="grid">
{cards}
</main>
</body>
</html>'''


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

    exclusive = find_exclusive(weekend_items, monday_items)
    print(f"{len(exclusive)} exclusive weekend items")

    (OUTPUT_DIR / "index.html").write_text(
        render_html(exclusive, monday_url, weekend_url, len(monday_items), len(weekend_items)),
        encoding="utf-8")
    if DEBUG:
        (OUTPUT_DIR / "debug_items.json").write_text(json.dumps(
            {"monday": [asdict(i) for i in monday_items],
             "weekend": [asdict(i) for i in weekend_items],
             "exclusive": [asdict(i) for i in exclusive]}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
