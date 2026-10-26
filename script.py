#!/usr/bin/env python3
"""
Scrape CocoWest.ca for Costco posts with robust category tagging and filtering,
smart date-aware post discovery, Vancouver time stamps, shopping list, store location,
clearance tags, end dates, discount % badges, sorting, list totals, emailable/shareable
shopping list, repeat-item markers, permanent post archiving, and full CocoWest SKU price history scraping.

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
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup

BASE_URL = "https://www.cocowest.ca/"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "public"))
ARCHIVE_DIR = OUTPUT_DIR / "archives"
DEBUG = os.getenv("DEBUG") == "1"
MATCH_MODE = os.getenv("MATCH_MODE", "smart").lower()

_owner, _, _repo = os.getenv("GITHUB_REPOSITORY", "").partition("/")
SITE_URL = os.getenv("SITE_URL") or (
    f"https://{_owner.lower()}.github.io/{_repo}/" if _owner and _repo else "")
if SITE_URL and not SITE_URL.endswith("/"):
    SITE_URL += "/"
STATE_URL = SITE_URL + "state.json" if SITE_URL else ""

WEEKEND_RE = re.compile(os.getenv("WEEKEND_RE", r"weekend[-_\s]*(?:sales[-_\s]*)?update"), re.I)
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

SALES_POST_RE = re.compile(r"(costco[-_\s]+flyer|sale[-_\s]+items)", re.I)

_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december")
_MONTH_ALIASES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
                  "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_PAT = "|".join(_MONTH_NAMES + tuple(sorted(_MONTH_ALIASES, key=len, reverse=True)))
RANGE_RE = re.compile(
    rf"\b({_MONTH_PAT})\.?\s+(\d{{1,2}})(?:\s*[-–—]\s*|\s+)(?:({_MONTH_PAT})\.?\s+)?(\d{{1,2}})\b[,\s]+(\d{{4}})",
    re.I)

def _month_num(name: str):
    n = (name or "").lower().rstrip(".")
    if n in _MONTH_NAMES:
        return _MONTH_NAMES.index(n) + 1
    return _MONTH_ALIASES.get(n)

def parse_range(text: str):
    m = RANGE_RE.search(text or "")
    if not m:
        return None
    m1, d1, m2, d2, year = m.groups()
    mo1 = _month_num(m1)
    mo2 = _month_num(m2) if m2 else mo1
    y = int(year)
    try:
        end = date(y, mo2, int(d2))
        start = date(y if mo2 >= mo1 else y - 1, mo1, int(d1))
    except (TypeError, ValueError):
        return None
    return (start, end) if start <= end else None

def range_from_html(post_html: str):
    soup = BeautifulSoup(post_html, "html.parser")
    texts = []
    h1 = soup.find("h1")
    if h1:
        texts.append(h1.get_text(" ", strip=True))
    if soup.title and soup.title.string:
        texts.append(soup.title.string)
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        texts.append(og["content"])
    for t in texts:
        r = parse_range(t)
        if r:
            return r
    return None

def fmt_range(rng) -> str:
    s, e = rng
    left = f"{s.strftime('%b')} {s.day}"
    same_month = e.month == s.month and e.year == s.year
    right = f"{e.day}" if same_month else f"{e.strftime('%b')} {e.day}"
    return f"{left} &ndash; {right}"

def weekend_over(rng, forced: bool, today) -> bool:
    effective_today = today - timedelta(days=1) if today.weekday() == 0 else today
    return (not forced) and bool(rng) and rng[1] < effective_today

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

    weekend_env = os.getenv("WEEKEND_URL")
    monday_env = os.getenv("MONDAY_URL")

    entries = [{"href": h, "blob": b, "order": n, "range": parse_range(b)}
               for n, (h, b) in enumerate(all_posts)]

    def newest(cands):
        dated = [c for c in cands if c["range"]]
        if dated:
            return max(dated, key=lambda c: (c["range"][1], -c["order"]))
        return cands[0] if cands else None

    weekend_pick = newest([e for e in entries if WEEKEND_RE.search(e["blob"])])
    weekend_url = weekend_env or (weekend_pick["href"] if weekend_pick else None)
    weekend_range = None
    if weekend_pick and weekend_pick["href"] == weekend_url:
        weekend_range = weekend_pick["range"]

    def is_candidate(e):
        return (e["href"] != weekend_url
                and not WEEKEND_RE.search(e["blob"])
                and not IGNORE_RE.search(e["blob"]))

    monday_pick = newest([e for e in entries
                          if is_candidate(e) and e["range"] and SALES_POST_RE.search(e["blob"])])
    if not monday_pick:
        monday_pick = next((e for e in entries if is_candidate(e)), None)

    if monday_env:
        monday_url, monday_range = monday_env, None
    elif monday_pick:
        monday_url, monday_range = monday_pick["href"], monday_pick["range"]
    elif all_posts:
        monday_url, monday_range = all_posts[0][0], None
    else:
        monday_url, monday_range = None, None

    return monday_url, weekend_url, {"monday": monday_range, "weekend": weekend_range}

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
    "groceries": re.compile(r"\b(coffee|protein|snack|meat|chicken|beef|pork|salmon|shrimp|bacon|sausage|juice|water|milk|cheese|chips|chocolate|cookie|bar|almond|rice|pasta|sauce|soup|oil|dressing|bread|cereal|yogurt|fruit|vegetable|salad|pizza|dumpling|egg|butter|nuts|honey|syrup|tea|wine|beer|spirits|kirkland|granola|tortilla|salsa|frozen|organic|crackers|oats|soda|coke|pepsi|torani|croissant|muffin|pie|cake|deli|ham|turkey|salami|dip|hummus|guacamole|tuna|tofu|broth|ketchup|mayo|mustard|spices|seasoning|strudel|strudels|chia|apple\s+juice|apple\s+sauce|apple\s+pie|steak|ribs|wings|burger|hotdog|noodle|ramen|tortellini|ravioli|ice\s*cream|gelato|sorbet|sparkling|lemonade|energy\s*drink|gatorade|electrolyte|pretzels|popcorn|cashew|pistachio|walnut|pecan|raisin|cranberry)\b", re.I),
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

PRICE_RE = re.compile(r"\$\s?(\d[\d,]*)\.(\d{2})\b")
END_RE = re.compile(
    r"(?:EXPIRES|ENDS|VALID\s+(?:UNTIL|THROUGH|THRU))\s+(?:ON\s+)?(\d{4}-\d{2}-\d{2})", re.I)

MONEY = r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)"
SAVE_RE = re.compile(r"\b(?:you\s+)?(?:instant\s+)?(?:save|savings?)\s*(?:of\s*)?:?\s*" + MONEY, re.I)
OFF_RE = re.compile(MONEY + r"\s*(?:off|instant\s+savings?|savings?|discount)\b", re.I)
REG_RE = re.compile(r"\b(?:reg(?:ular)?|was|orig(?:inal)?)\.?\s*(?:price)?\s*:?\s*" + MONEY, re.I)
PCT_OFF_RE = re.compile(r"\b(\d{1,2})\s*%\s*off\b", re.I)

def parse_price(label: str):
    text = re.sub(r"\([^)]*\)", " ", label or "")
    found = PRICE_RE.findall(text)
    return found[-1] if found else None

def strip_savings(label: str) -> str:
    text = label or ""
    for rx in (SAVE_RE, OFF_RE, REG_RE):
        text = rx.sub(" ", text)
    return text

def price_value(label: str):
    p = parse_price(strip_savings(label))
    if not p:
        return None
    return float(f"{p[0].replace(',', '')}.{p[1]}")

def is_clearance(label: str) -> bool:
    p = parse_price(strip_savings(label))
    return bool(p) and p[1] == "97"

def _money(s: str) -> float:
    return float(s.replace(",", ""))

def _sane_pct(pct):
    return pct if pct is not None and 1 <= pct <= 90 else None

def discount_pct(label: str):
    label = label or ""
    m = PCT_OFF_RE.search(label)
    if m:
        return _sane_pct(int(m.group(1)))

    sale = price_value(label)
    if not sale:
        return None

    m = REG_RE.search(label)
    if m:
        reg = _money(m.group(1))
        if reg > sale:
            return _sane_pct(round((reg - sale) / reg * 100))

    m = SAVE_RE.search(label) or OFF_RE.search(label)
    if m:
        save = _money(m.group(1))
        if save > 0:
            return _sane_pct(round(save / (sale + save) * 100))
    return None

def parse_end_date(label: str) -> str:
    m = END_RE.search(label or "")
    if not m:
        return ""
    try:
        datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return ""
    return m.group(1)

def item_ident(it: Item) -> str:
    return f"no:{it.item_no}" if it.item_no else (it.file_key or it.alt_key or it.url)

def _previous_entry(prev_state, mode, target_url):
    entry = (prev_state.get("history") or {}).get(mode) or {}
    cur = entry.get("current") or {}
    if cur.get("url") == target_url:
        return entry.get("previous") or {}
    return cur

def previous_ids(prev_state, mode, target_url):
    return set(_previous_entry(prev_state, mode, target_url).get("ids") or [])

def build_history(prev_state, mode, target_url, ids):
    history = dict(prev_state.get("history") or {})
    history[mode] = {
        "current": {"url": target_url, "ids": ids},
        "previous": _previous_entry(prev_state, mode, target_url),
    }
    return history

def scrape_cocowest_item_history(scraper, item_no: str):
    """Fetches full historical price table from cocowest.ca/item/<item_no>"""
    url = f"{BASE_URL}item/{item_no}"
    try:
        html_text = fetch(scraper, url, retries=2)
        soup = BeautifulSoup(html_text, "html.parser")
        
        records = []
        rows = soup.find_all("tr")
        for tr in rows:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(tds) >= 4:
                date_posted = tds[0].strip()
                p_str = tds[3].replace("$", "").replace(",", "").strip()
                
                # Ensure the posted date matches YYYY-MM-DD format
                if re.match(r"^\d{4}-\d{2}-\d{2}$", date_posted):
                    try:
                        price = float(p_str)
                        records.append({"date": date_posted, "price": price, "pct": None})
                    except ValueError:
                        continue
                        
        return records
    except Exception as exc:
        print(f"[history] Could not fetch history for SKU {item_no}: {exc!r}", file=sys.stderr)
        return []

def update_price_db(scraper, prev_state, target_items, valid_range):
    """Accumulates SKU price history, deep-scraping cocowest.ca/item/<sku> when available."""
    db = dict(prev_state.get("price_db") or {})
    today_str = datetime.now(ZoneInfo("America/Vancouver")).strftime("%Y-%m-%d")
    date_label = valid_range[0].isoformat() if valid_range else today_str

    for i in target_items:
        ident = item_ident(i)
        price = price_value(i.label)
        pct = discount_pct(i.label)
        entry = db.get(ident, {"name": i.label, "item_no": i.item_no, "history": [], "full_scraped": False})

        # Deep scrape history from CocoWest item page if we haven't already
        if i.item_no and (not entry.get("full_scraped") or len(entry.get("history", [])) <= 1):
            print(f"[history] Deep scraping CocoWest price history for SKU {i.item_no}...")
            remote_records = scrape_cocowest_item_history(scraper, i.item_no)
            if remote_records:
                # Merge remote records, avoiding duplicate dates
                existing_dates = {h["date"] for h in entry["history"]}
                for r in remote_records:
                    if r["date"] not in existing_dates:
                        entry["history"].append(r)
                # Sort history chronologically
                entry["history"].sort(key=lambda x: x["date"])
            entry["full_scraped"] = True

        if price is not None:
            hist = entry["history"]
            if not hist or hist[-1]["date"] != date_label:
                hist.append({"date": date_label, "price": price, "pct": pct})
        db[ident] = entry
    return db

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

def digest_line(it: Item) -> str:
    pct = discount_pct(it.label)
    price = price_value(it.label)
    bits = []
    if pct:
        bits.append(f"{pct}% off")
    if price is not None:
        bits.append(f"${price:.2f}")
    name = re.sub(r"\s+", " ", it.label).strip()[:90]
    return f"- {name}" + (f" ({', '.join(bits)})" if bits else "")

def build_digest(items, exclusive, is_weekend_mode):
    sections = []

    with_disc = sorted((i for i in items if discount_pct(i.label)),
                       key=lambda i: discount_pct(i.label), reverse=True)
    if with_disc:
        sections.append("BIGGEST DISCOUNTS\n" + "\n".join(digest_line(i) for i in with_disc[:5]))

    if is_weekend_mode and exclusive:
        top_new = sorted(exclusive, key=lambda i: discount_pct(i.label) or 0, reverse=True)[:5]
        sections.append("NEW THIS WEEKEND\n" + "\n".join(digest_line(i) for i in top_new))

    clearance = [i for i in items if is_clearance(i.label)]
    if clearance:
        shown = clearance[:8]
        extra = len(clearance) - len(shown)
        text = "CLEARANCE FINDS (.97 prices)\n" + "\n".join(digest_line(i) for i in shown)
        if extra > 0:
            text += f"\n...and {extra} more on the site"
        sections.append(text)

    return "\n\n".join(sections)

def maybe_notify(prev_state, state_ok, target_url, is_weekend_mode, items, exclusive):
    force = os.getenv("FORCE_NOTIFY") == "1"
    key = "weekend_url" if is_weekend_mode else "flyer_url"
    is_new_post = target_url != prev_state.get(key)
    if not (force or (is_new_post and state_ok)):
        print("[email] not sending notification")
        return

    items_count = len(items)
    exclusive_count = len(exclusive)
    mode_name = "Weekend Exclusives" if is_weekend_mode else "Monday Sales Post"
    lines = [
        f"CocoWest {mode_name} updated: {items_count} items found.",
        f"Browse them: {SITE_URL or '(site URL unknown)'}",
        f"Post URL: {target_url}",
    ]
    digest = build_digest(items, exclusive, is_weekend_mode)
    if digest:
        lines += ["", digest]

    best = max((discount_pct(i.label) or 0 for i in items), default=0)
    best_txt = f" | top deal {best}% off" if best else ""
    if is_weekend_mode:
        subj = f"CocoWest: {exclusive_count} new weekend deals{best_txt}"
    else:
        subj = f"CocoWest: Monday Sales Post ({items_count} items){best_txt}"
    send_email(subj, "\n".join(lines))

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
         padding-bottom:4.5rem; padding-left:env(safe-area-inset-left,0px); padding-right:env(safe-area-inset-right,0px); }
  
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
  .sort-select { border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:999px;
                 padding:.25rem .5rem; font-size:.8rem; font-weight:600; max-width:100%; }
  .cols-toggle { display:none; align-items:center; gap:.35rem; margin-left:auto; font-size:.8rem; color:var(--muted); }
  .seg { display:inline-flex; border:1px solid var(--line); border-radius:999px; overflow:hidden; }
  .seg button { border:none; background:var(--card); color:var(--fg); padding:.3rem .7rem; font-size:.85rem; font-weight:700; cursor:pointer; }
  .seg button.active { background:var(--accent); color:var(--on-accent); }

  .list-bar { max-width:1200px; margin:0 auto; padding:0 .8rem .6rem; display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; }
  .list-sum { font-size:.85rem; font-weight:700; margin-right:auto; }
  
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
  .tags-right { position:absolute; top:8px; right:8px; z-index:2; display:flex; flex-direction:column; align-items:flex-end;
                gap:4px; pointer-events:none; }
  .disc-tag { background:#d93025; color:#fff; font-size:.8rem; font-weight:800; padding:2px 7px; border-radius:4px;
              box-shadow:0 1px 3px rgba(0,0,0,.35); }
  .clr-tag { background:#6f42c1; color:#fff; font-size:.7rem; font-weight:bold; padding:2px 6px; border-radius:4px;
             text-transform:uppercase; }
  .card a { display:block; }
  .card img { width:100%; aspect-ratio:1/1; object-fit:contain; display:block; background:#fff; }
  .card figcaption { padding:.5rem .6rem; font-size:.85rem; line-height:1.3; flex-grow:1; overflow-wrap:anywhere; cursor:pointer; }
  .card-footer { padding:.4rem .6rem .6rem; display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:.3rem;
                 border-top:1px solid var(--line); font-size:.8rem; }
  .shop-label { display:flex; align-items:center; gap:.4rem; cursor:pointer; font-weight:500; }
  .shop-label input { width:1.1rem; height:1.1rem; accent-color:var(--accent); cursor:pointer; }
  .foot-right { display:flex; align-items:center; gap:.3rem; flex-wrap:wrap; justify-content:flex-end; margin-left:auto; }
  .sku-badge { font-size:.68rem; font-weight:700; padding:2px 6px; border-radius:4px; background:var(--line); color:var(--fg); cursor:pointer; }
  .repeat-tag { font-size:.68rem; font-weight:600; padding:2px 7px; border-radius:999px; border:1px solid var(--line);
                color:var(--muted); white-space:nowrap; }
  .end-badge { font-size:.72rem; font-weight:700; padding:2px 8px; border-radius:999px; white-space:nowrap; }
  .end-badge.today { background:#dc3545; color:#fff; }
  .end-badge.tomorrow { background:#fd7e14; color:#fff; }
  .end-badge.week { background:#ffc107; color:#1a1a1a; }
  .end-badge.later { background:transparent; color:var(--muted); border:1px solid var(--line); font-weight:600; }
  .end-badge.expired { background:var(--line); color:var(--muted); text-decoration:line-through; }
  .empty { padding:2rem 1rem; text-align:center; color:var(--muted); grid-column:1/-1; }

  .modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,.6); backdrop-filter:blur(4px); z-index:100; display:flex; align-items:center; justify-content:center; padding:1rem; }
  .modal { background:var(--card); color:var(--fg); border-radius:14px; max-width:450px; width:100%; max-height:85vh; overflow-y:auto; padding:1.2rem; box-shadow:0 8px 24px rgba(0,0,0,.3); position:relative; }
  .modal-close { position:absolute; top:12px; right:12px; border:none; background:none; font-size:1.4rem; cursor:pointer; color:var(--muted); }
  .hist-list { list-style:none; padding:0; margin:.8rem 0 0; }
  .hist-item { display:flex; justify-content:space-between; padding:.5rem 0; border-bottom:1px solid var(--line); font-size:.85rem; }

  .top-btn { position:fixed; right:calc(.8rem + env(safe-area-inset-right,0px)); bottom:calc(.8rem + env(safe-area-inset-bottom,0px));
             width:2.9rem; height:2.9rem; border-radius:50%; border:none; background:var(--accent); color:var(--on-accent);
             font-size:1.5rem; font-weight:700; line-height:1; cursor:pointer; box-shadow:0 2px 8px rgba(0,0,0,.35);
             z-index:20; opacity:0; pointer-events:none; transition:opacity .2s; }
  .top-btn.show { opacity:1; pointer-events:auto; }

  @media (max-width:640px) {
    .cols-toggle { display:flex; }
    .toolbar { padding:0 .6rem .5rem; gap:.25rem; }
    .list-bar { padding:0 .6rem .5rem; gap:.3rem; }
    .chip { font-size:.72rem; padding:.22rem .5rem; }
    .chip.icon-only { font-size:.85rem; padding:.22rem .4rem; }
    .sort-select { font-size:.72rem; padding:.22rem .4rem; }
    .grid { padding:0 .5rem; gap:.5rem; }
    .grid.c1 { grid-template-columns:minmax(0,1fr); }
    .grid.c2 { grid-template-columns:repeat(2,minmax(0,1fr)); }
    .grid.c2 .card { border-radius:10px; }
    .grid.c2 .card figcaption { font-size:.72rem; padding:.4rem .45rem; }
    .grid.c2 .card-footer { padding:.35rem .45rem .5rem; font-size:.72rem; }
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
    <button class="chip" id="wkhide-toggle" type="button" aria-pressed="false" hidden>&#128683; Hide ending this week (0)</button>
    <button class="chip" id="exp-toggle" type="button" aria-pressed="false" hidden>&#128584; Hide expired (0)</button>
    __REPEAT_BTN__
    <button class="chip icon-only" data-cat="appliances" type="button" aria-pressed="false" title="Appliances">&#129368;</button>
    <button class="chip icon-only" data-cat="electronics" type="button" aria-pressed="false" title="Electronics">&#128187;</button>
    <button class="chip icon-only" data-cat="groceries" type="button" aria-pressed="false" title="Groceries">&#129382;</button>
    <button class="chip icon-only" data-cat="household" type="button" aria-pressed="false" title="Household">&#129532;</button>
    <button class="chip icon-only" data-cat="clothing" type="button" aria-pressed="false" title="Clothing">&#128084;</button>
    <button class="chip icon-only" data-cat="health" type="button" aria-pressed="false" title="Health">&#128138;</button>
    <button class="chip icon-only" data-cat="home" type="button" aria-pressed="false" title="Home">&#129692;</button>
    <select class="sort-select" id="sort" aria-label="Sort items">
      <option value="default">Sort: Default</option>
      <option value="disc">Biggest discount</option>
      <option value="plow">Price: low to high</option>
      <option value="phigh">Price: high to low</option>
      <option value="end">Ending soonest</option>
      <option value="endlate">Ending latest</option>
      <option value="cat">Category</option>
    </select>
    <div class="cols-toggle">
      <span>Per row</span>
      <span class="seg">
        <button type="button" data-cols="1" aria-pressed="false">1</button>
        <button type="button" data-cols="2" aria-pressed="true">2</button>
      </span>
    </div>
  </div>
  <div class="list-bar" id="list-bar" hidden>
    <span class="list-sum" id="list-sum"></span>
    <button class="chip" id="email-list" type="button">&#9993;&#65039; Email</button>
    <button class="chip" id="share-list" type="button">&#128279; Share</button>
    <button class="chip danger" id="clear-list" type="button">Clear</button>
  </div>
</div>
<div class="meta">
  📍 Store Location: <b>__LOCATION__</b><br>
  __VALID_LINE__
  Showing <b id="shown-count">__TOTAL__</b> of <b>__TOTAL__</b> items &middot; updated __STAMP__<br>
  Source: <a href="__SOURCE_URL__">__SOURCE_NAME__</a>
</div>
<main class="grid c2" id="grid">
__CARDS__
<p class="empty" id="no-match" hidden></p>
</main>

<div class="modal-overlay" id="sku-modal" hidden>
  <div class="modal">
    <button class="modal-close" id="modal-close">&times;</button>
    <h3 id="modal-title" style="margin-top:0;">Price History</h3>
    <div id="modal-body">Loading SKU history...</div>
  </div>
</div>

<button class="top-btn" id="top-btn" type="button" aria-label="Back to top" title="Back to top">&#8593;</button>
<script>
(function () {
  var LIST_KEY = 'cocowest_shopping_list';
  var COLS_KEY = 'cocowest_cols';
  var HIDE_EXP_KEY = 'cocowest_hide_expired';
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var NL = String.fromCharCode(10);

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

  function hashUrl(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) { h = ((h << 5) + h + s.charCodeAt(i)) | 0; }
    return (h >>> 0).toString(36);
  }

  var q = document.getElementById('q');
  var grid = document.getElementById('grid');
  var listBtn = document.getElementById('list-toggle');
  var newBtn = document.getElementById('new-toggle');
  var clrBtn = document.getElementById('clr-toggle');
  var weekBtn = document.getElementById('week-toggle');
  var wkHideBtn = document.getElementById('wkhide-toggle');
  var expBtn = document.getElementById('exp-toggle');
  var repBtn = document.getElementById('rep-toggle');
  var sortSel = document.getElementById('sort');
  var catBtns = Array.prototype.slice.call(document.querySelectorAll('[data-cat]'));
  var listBar = document.getElementById('list-bar');
  var listSum = document.getElementById('list-sum');
  var emailBtn = document.getElementById('email-list');
  var shareBtn = document.getElementById('share-list');
  var clearBtn = document.getElementById('clear-list');
  var resetBtn = document.getElementById('reset-btn');
  var topBtn = document.getElementById('top-btn');
  var shownEl = document.getElementById('shown-count');
  var emptyMsg = document.getElementById('no-match');
  var colBtns = Array.prototype.slice.call(document.querySelectorAll('[data-cols]'));
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var checks = Array.prototype.slice.call(document.querySelectorAll('.shop-check'));
  var modal = document.getElementById('sku-modal');
  var modalClose = document.getElementById('modal-close');
  var modalTitle = document.getElementById('modal-title');
  var modalBody = document.getElementById('modal-body');
  var origOrder = cards.slice();

  var hashToUrl = {};
  cards.forEach(function(card) {
    var ident = card.getAttribute('data-ident');
    var name = card.getAttribute('data-name');
    var sku = card.getAttribute('data-sku');
    var trigger = card.querySelector('figcaption');
    var skuBadge = card.querySelector('.sku-badge');

    function renderHistoryList(historyArr) {
      if (!historyArr || !historyArr.length) {
        modalBody.innerHTML = '<p style="color:var(--muted);font-size:.85rem;">No historical price data found.</p>';
        return;
      }
      var html = '<p style="color:var(--muted);font-size:.85rem;">Recorded Price History:</p><ul class="hist-list">';
      historyArr.slice().reverse().forEach(function(h) {
        html += '<li class="hist-item"><span><b>' + h.date + '</b></span><span>$' + Number(h.price).toFixed(2) + (h.pct ? ' (' + h.pct + '% off)' : '') + '</span></li>';
      });
      html += '</ul>';
      modalBody.innerHTML = html;
    }

    function showModal() {
      modalTitle.textContent = name;
      modalBody.innerHTML = '<p style="color:var(--muted);font-size:.85rem;">Loading live price history...</p>';
      modal.hidden = false;

      // 1. First check if we already have multi-row history in state.json
      fetchPriceDb(function(db) {
        var item = db[ident];
        if (item && item.history && item.history.length > 1) {
          renderHistoryList(item.history);
          return;
        }

        // 2. If not, fetch the live item page via public CORS proxy on demand
        if (!sku) {
          if (item && item.history) {
            renderHistoryList(item.history);
          } else {
            modalBody.innerHTML = '<p style="color:var(--muted);font-size:.85rem;">No SKU available to look up history.</p>';
          }
          return;
        }

        var targetUrl = 'https://www.cocowest.ca/item/' + sku;
        var proxyUrl = 'https://api.allorigins.win/get?url=' + encodeURIComponent(targetUrl);

        fetch(proxyUrl)
          .then(function(res) { return res.json(); })
          .then(function(data) {
            if (!data || !data.contents) throw new Error('Empty proxy response');

            var parser = new DOMParser();
            var doc = parser.parseFromString(data.contents, 'text/html');
            var rows = Array.prototype.slice.call(doc.querySelectorAll('tr'));
            var records = [];

            rows.forEach(function(tr) {
              var tds = Array.prototype.slice.call(tr.querySelectorAll('td, th')).map(function(td) {
                return td.textContent.trim();
              });
              if (tds.length >= 4) {
                var datePosted = tds[0];
                var pStr = tds[3].replace('$', '').replace(',', '').trim();
                if (/^\d{4}-\d{2}-\d{2}$/.test(datePosted)) {
                  var price = parseFloat(pStr);
                  if (!isNaN(price)) {
                    records.push({ date: datePosted, price: price, pct: null });
                  }
                }
              }
            });

            if (records.length > 0) {
              renderHistoryList(records);
            } else if (item && item.history && item.history.length) {
              renderHistoryList(item.history);
            } else {
              modalBody.innerHTML = '<p style="color:var(--muted);font-size:.85rem;">No historical price table found on item page.</p>';
            }
          })
          .catch(function(err) {
            if (item && item.history) {
              renderHistoryList(item.history);
            } else {
              modalBody.innerHTML = '<p style="color:var(--muted);font-size:.85rem;">Could not load live history.</p>';
            }
          });
      });
    }

    if (trigger) trigger.addEventListener('click', showModal);
    if (skuBadge) skuBadge.addEventListener('click', showModal);
  });

  var priceDb = null;
  function fetchPriceDb(cb) {
    if (priceDb) return cb(priceDb);
    fetch('state.json?t=' + Date.now()).then(function(res){ return res.json(); }).then(function(data){
      priceDb = data.price_db || {};
      cb(priceDb);
    }).catch(function(){ cb({}); });
  }

  cards.forEach(function(card) {
    var ident = card.getAttribute('data-ident');
    var name = card.getAttribute('data-name');
    var trigger = card.querySelector('figcaption');
    var skuBadge = card.querySelector('.sku-badge');
    function showModal() {
      modalTitle.textContent = name;
      modalBody.innerHTML = 'Loading SKU history...';
      modal.hidden = false;
      fetchPriceDb(function(db) {
        var item = db[ident];
        if (!item || !item.history || !item.history.length) {
          modalBody.innerHTML = '<p>No historical price data recorded yet for this item.</p>';
          return;
        }
        var html = '<p style="color:var(--muted);font-size:.85rem;">Recorded Price History:</p><ul class="hist-list">';
        item.history.slice().reverse().forEach(function(h) {
          html += '<li class="hist-item"><span><b>' + h.date + '</b></span><span>$' + Number(h.price).toFixed(2) + (h.pct ? ' (' + h.pct + '% off)' : '') + '</span></li>';
        });
        html += '</ul>';
        modalBody.innerHTML = html;
      });
    }
    if (trigger) trigger.addEventListener('click', showModal);
    if (skuBadge) skuBadge.addEventListener('click', showModal);
  });

  modalClose.addEventListener('click', function() { modal.hidden = true; });
  modal.addEventListener('click', function(e) { if (e.target === modal) modal.hidden = true; });

  var shoppingList = load(LIST_KEY, []);
  if (!Array.isArray(shoppingList)) shoppingList = [];
  var state = {
    listOnly: false, newOnly: false, clrOnly: false, weekOnly: false, hideWeek: false,
    hideExp: !!load(HIDE_EXP_KEY, false), hideRep: false, activeCat: '', sort: 'default'
  };

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
  function num(el, attr) {
    var v = parseFloat(el.getAttribute(attr));
    return isNaN(v) ? null : v;
  }

  if (newBtn) {
    newBtn.innerHTML = '&#10024; New (' + countWhere(function (el) { return el.classList.contains('is-new'); }) + ')';
  }
  clrBtn.innerHTML = '&#127991;&#65039; Clearance (' + countWhere(function (el) { return el.classList.contains('is-clr'); }) + ')';
  var weekCount = countWhere(function (el) { return el.getAttribute('data-week') === '1'; });
  weekBtn.innerHTML = '&#9200; Ends this week (' + weekCount + ')';
  if (weekCount > 0) {
    wkHideBtn.hidden = false;
    wkHideBtn.innerHTML = '&#128683; Hide ending this week (' + weekCount + ')';
  } else {
    wkHideBtn.hidden = true;
  }

  var expCount = countWhere(function (el) { return el.classList.contains('is-expired'); });
  if (expCount > 0) {
    expBtn.hidden = false;
    expBtn.innerHTML = '&#128584; Hide expired (' + expCount + ')';
  } else {
    expBtn.hidden = true;
    state.hideExp = false;
  }

  function inList(url) { return shoppingList.indexOf(url) !== -1; }

  function listCountOnPage() {
    return countWhere(function (el) { return inList(el.getAttribute('data-url')); });
  }

  function listItems() {
    return origOrder.filter(function (el) { return inList(el.getAttribute('data-url')); });
  }

  function setToggle(btn, active) {
    if (!btn) return;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  }

  function applySort() {
    var key = state.sort;
    var list = origOrder.slice();
    function cmpNullLast(a, b, dir) {
      if (a === null && b === null) return 0;
      if (a === null) return 1;
      if (b === null) return -1;
      return dir * (a - b);
    }
    list.sort(function (a, b) {
      var r = 0;
      if (key === 'disc') {
        r = cmpNullLast(num(a, 'data-disc'), num(b, 'data-disc'), -1);
      } else if (key === 'plow') {
        r = cmpNullLast(num(a, 'data-price'), num(b, 'data-price'), 1);
      } else if (key === 'phigh') {
        r = cmpNullLast(num(a, 'data-price'), num(b, 'data-price'), -1);
      } else if (key === 'end') {
        var xa = a.classList.contains('is-expired'), xb = b.classList.contains('is-expired');
        var ea = a.getAttribute('data-end'), eb = b.getAttribute('data-end');
        if (xa !== xb) r = xa ? 1 : -1;
        else if (ea && !eb) r = -1;
        else if (!ea && eb) r = 1;
        else if (ea && eb) r = ea < eb ? -1 : (ea > eb ? 1 : 0);
      } else if (key === 'endlate') {
        var la = a.getAttribute('data-end'), lb = b.getAttribute('data-end');
        if (la && !lb) r = -1;
        else if (!la && lb) r = 1;
        else if (la && lb) r = la > lb ? -1 : (la < lb ? 1 : 0);
      } else if (key === 'cat') {
        var ca = a.getAttribute('data-cat'), cb = b.getAttribute('data-cat');
        r = ca < cb ? -1 : (ca > cb ? 1 : 0);
      }
      return r || (Number(a.getAttribute('data-idx')) - Number(b.getAttribute('data-idx')));
    });
    list.forEach(function (el) { grid.appendChild(el); });
    grid.appendChild(emptyMsg);
  }

  function render() {
    var t = q.value.trim().toLowerCase();
    var shown = 0;
    cards.forEach(function (el) {
      var ok = (!t || el.getAttribute('data-t').indexOf(t) !== -1) &&
               (!state.newOnly || el.classList.contains('is-new')) &&
               (!state.clrOnly || el.classList.contains('is-clr')) &&
               (!state.weekOnly || el.getAttribute('data-week') === '1') &&
               (!state.hideWeek || el.getAttribute('data-week') !== '1') &&
               (!state.hideExp || !el.classList.contains('is-expired')) &&
               (!state.hideRep || el.getAttribute('data-seen') !== '1') &&
               (!state.activeCat || el.getAttribute('data-cat') === state.activeCat) &&
               (!state.listOnly || inList(el.getAttribute('data-url')));
      el.hidden = !ok;
      if (ok) shown++;
    });

    var n = 0, sum = 0, unpriced = 0;
    cards.forEach(function (el) {
      if (!inList(el.getAttribute('data-url'))) return;
      n++;
      var p = num(el, 'data-price');
      if (p === null) unpriced++; else sum += p;
    });

    listBtn.innerHTML = state.listOnly ? 'Show all items' : '&#128722; List (' + n + ')';
    listBtn.classList.toggle('active', state.listOnly);

    setToggle(newBtn, state.newOnly);
    setToggle(clrBtn, state.clrOnly);
    setToggle(weekBtn, state.weekOnly);
    setToggle(wkHideBtn, state.hideWeek);
    setToggle(expBtn, state.hideExp);
    setToggle(repBtn, state.hideRep);

    catBtns.forEach(function (btn) {
      setToggle(btn, state.activeCat === btn.getAttribute('data-cat'));
    });

    listBar.hidden = n === 0;
    listSum.textContent = n + (n === 1 ? ' item' : ' items') + ' · $' + sum.toFixed(2) +
                          (unpriced ? ' (+' + unpriced + ' unpriced)' : '');
    shownEl.textContent = shown;

    if (shown === 0) {
      emptyMsg.textContent = (state.listOnly && n === 0)
        ? 'Your list is empty. Tick "Add to list" on any item.'
        : 'No items match your filters.';
    }
    emptyMsg.hidden = shown !== 0;
  }

  (function importShared() {
    var m = /[#&]list=([0-9a-z.]+)/.exec(location.hash || '');
    if (!m) return;
    var urls = [];
    m[1].split('.').forEach(function (h) { if (hashToUrl[h]) urls.push(hashToUrl[h]); });
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}
    if (!urls.length) {
      window.alert('That shared list does not match any items on this page. It may be from an older post.');
      return;
    }
    if (!window.confirm('Add ' + urls.length + ' shared item' + (urls.length === 1 ? '' : 's') + ' to your list?')) return;
    urls.forEach(function (u) { if (!inList(u)) shoppingList.push(u); });
    save(LIST_KEY, shoppingList);
    state.listOnly = true;
  })();

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

  function shareUrl() {
    var base = location.href.split('#')[0];
    var hashes = listItems().map(function (el) { return hashUrl(el.getAttribute('data-url')); });
    return base + '#list=' + hashes.join('.');
  }

  function itemLine(el) {
    var name = (el.getAttribute('data-name') || '').replace(/ +/g, ' ').trim();
    if (name.length > 100) name = name.slice(0, 97) + '...';
    var p = num(el, 'data-price');
    var d = num(el, 'data-disc');
    var end = el.getAttribute('data-end');
    var extra = [];
    if (p !== null) extra.push('$' + p.toFixed(2));
    if (d !== null) extra.push(d + '% off');
    if (end) extra.push('ends ' + end);
    return '- ' + name + (extra.length ? ' (' + extra.join(', ') + ')' : '');
  }

  emailBtn.addEventListener('click', function () {
    var items = listItems();
    if (!items.length) return;
    var sum = 0;
    items.forEach(function (el) { var p = num(el, 'data-price'); if (p !== null) sum += p; });
    var head = 'My Costco shopping list (' + items.length + ' item' + (items.length === 1 ? '' : 's') + '):' + NL + NL +
               'Open it on the site: ' + shareUrl() + NL + NL;
    var tail = NL + 'Total: $' + sum.toFixed(2);
    var lines = [];
    var skipped = 0;
    items.forEach(function (el) {
      var line = itemLine(el);
      var test = head + lines.concat([line]).join(NL) + tail;
      if (encodeURIComponent(test).length < 4000) lines.push(line); else skipped++;
    });
    var body = head + lines.join(NL) + (skipped ? NL + '...and ' + skipped + ' more (use the link above)' : '') + tail;
    window.location.href = 'mailto:?subject=' + encodeURIComponent('Costco shopping list') +
                           '&body=' + encodeURIComponent(body);
  });

  shareBtn.addEventListener('click', function () {
    var items = listItems();
    if (!items.length) return;
    var url = shareUrl();
    if (navigator.share) {
      navigator.share({ title: 'Costco shopping list', text: items.length + ' items', url: url }).catch(function () {});
      return;
    }
    function fallback() { window.prompt('Copy this link:', url); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () { window.alert('Link copied.'); }, fallback);
    } else {
      fallback();
    }
  });

  listBtn.addEventListener('click', function () { state.listOnly = !state.listOnly; render(); });
  if (newBtn) {
    newBtn.addEventListener('click', function () { state.newOnly = !state.newOnly; render(); });
  }
  if (repBtn) {
    repBtn.addEventListener('click', function () { state.hideRep = !state.hideRep; render(); });
  }
  clrBtn.addEventListener('click', function () { state.clrOnly = !state.clrOnly; render(); });
  weekBtn.addEventListener('click', function () {
    state.weekOnly = !state.weekOnly;
    if (state.weekOnly) state.hideWeek = false;
    render();
  });
  wkHideBtn.addEventListener('click', function () {
    state.hideWeek = !state.hideWeek;
    if (state.hideWeek) state.weekOnly = false;
    render();
  });
  expBtn.addEventListener('click', function () {
    state.hideExp = !state.hideExp;
    save(HIDE_EXP_KEY, state.hideExp);
    render();
  });

  sortSel.addEventListener('change', function () {
    state.sort = sortSel.value;
    applySort();
    render();
  });
  
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
    state.hideWeek = false;
    state.hideExp = false;
    state.hideRep = false;
    state.listOnly = false;
    state.activeCat = '';
    state.sort = 'default';
    sortSel.value = 'default';
    save(HIDE_EXP_KEY, false);
    applySort();
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

  function onScroll() {
    var y = window.pageYOffset || document.documentElement.scrollTop || 0;
    topBtn.classList.toggle('show', y > 500);
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  topBtn.addEventListener('click', function () {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  onScroll();

  render();
})();
</script>
</body>
</html>"""

def render_html(items, monday_url, weekend_url, store_location, is_weekend_mode,
                exclusive_urls=None, seen_ids=None, valid_range=None, stale=False) -> str:
    stamp = datetime.now(ZoneInfo("America/Vancouver")).strftime("%b %d, %I:%M %p")
    esc = html.escape
    exclusive_urls = exclusive_urls or set()
    seen_ids = seen_ids or set()
    
    clr_count = sum(1 for i in items if is_clearance(i.label))
    new_count = len(exclusive_urls)
    repeat_count = sum(1 for i in items if item_ident(i) in seen_ids)

    if valid_range and stale:
        valid_line = (f"&#9203; <b>Waiting for this week's post</b> &ndash; showing the one for "
                      f"{fmt_range(valid_range)} (ended)<br>")
    elif valid_range:
        valid_line = f"&#128197; Valid <b>{fmt_range(valid_range)}</b><br>"
    else:
        valid_line = ""

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

    if repeat_count:
        repeat_btn_html = (f'<button class="chip" id="rep-toggle" type="button" aria-pressed="false" '
                           f'title="Hide items that were also on last week\'s post">'
                           f'&#128257; Hide repeats ({repeat_count})</button>')
    else:
        repeat_btn_html = ''

    cards = []
    for n, i in enumerate(items):
        is_new = i.url in exclusive_urls
        clearance = is_clearance(i.label)
        end = parse_end_date(i.label)
        pct = discount_pct(i.label)
        price = price_value(i.label)
        ident = item_ident(i)
        is_repeat = ident in seen_ids

        classes = ["card"]
        if is_new:
            classes.append("is-new")
        if clearance:
            classes.append("is-clr")
        class_str = " ".join(classes)

        attrs = (f'data-t="{esc(i.label.lower(), quote=True)}" '
             f'data-name="{esc(i.label, quote=True)}" '
             f'data-url="{esc(i.url, quote=True)}" '
             f'data-ident="{esc(ident, quote=True)}" '
             f'data-sku="{esc(i.item_no or "")}" '
             f'data-cat="{esc(i.category, quote=True)}" '
             f'data-idx="{n}"')
        if end:
            attrs += f' data-end="{end}"'
        if price is not None:
            attrs += f' data-price="{price:.2f}"'
        if pct:
            attrs += f' data-disc="{pct}"'
        if is_repeat:
            attrs += ' data-seen="1"'

        new_tag = '<div class="new-tag">&#10024; New</div>' if is_new else ''
        disc_tag = f'<div class="disc-tag">-{pct}%</div>' if pct else ''
        clr_tag = '<div class="clr-tag">&#127991;&#65039; Clearance</div>' if clearance else ''
        right_tags = f'<div class="tags-right">{disc_tag}{clr_tag}</div>' if (pct or clearance) else ''
        end_badge = '<span class="end-badge" hidden></span>' if end else ''
        repeat_tag = ('<span class="repeat-tag" title="Also on last week\'s post">&#128257; Repeat</span>'
                      if is_repeat else '')
        sku_badge = f'<span class="sku-badge" title="View SKU history">#{esc(i.item_no)}</span>' if i.item_no else ''
        foot_right = f'<span class="foot-right">{sku_badge}{repeat_tag}{end_badge}</span>'

        cards.append(f'''<figure class="{class_str}" {attrs}>
  {new_tag}{right_tags}
  <a href="{esc(i.url, quote=True)}" target="_blank" rel="noopener">
    <img src="{esc(i.url, quote=True)}" alt="{esc(i.label, quote=True)}" loading="lazy" referrerpolicy="no-referrer">
  </a>
  <figcaption title="Click to view SKU price history">{esc(i.label)}</figcaption>
  <div class="card-footer">
    <label class="shop-label">
      <input type="checkbox" class="shop-check" data-url="{esc(i.url, quote=True)}"> Add to list
    </label>
    {foot_right}
  </div>
</figure>''')

    cards_html = "\n".join(cards) or '<p class="empty">No items found.</p>'

    return (PAGE_TEMPLATE
            .replace("__PAGE_TITLE__", page_title)
            .replace("__TOTAL__", str(len(items)))
            .replace("__CLR_COUNT__", str(clr_count))
            .replace("__NEW_BTN__", new_btn_html)
            .replace("__REPEAT_BTN__", repeat_btn_html)
            .replace("__LOCATION__", esc(store_location or "Metro Vancouver Area"))
            .replace("__VALID_LINE__", valid_line)
            .replace("__STAMP__", stamp)
            .replace("__SOURCE_URL__", esc(source_url, quote=True))
            .replace("__SOURCE_NAME__", source_name)
            .replace("__CARDS__", cards_html))

def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    scraper = make_scraper()

    today = datetime.now(ZoneInfo("America/Vancouver")).date()
    weekend_forced = bool(os.getenv("WEEKEND_URL"))
    ranges = {}
    monday_url, weekend_url = os.getenv("MONDAY_URL"), os.getenv("WEEKEND_URL")
    if not monday_url:
        home = fetch(scraper, BASE_URL)
        monday_url, weekend_url, ranges = discover_posts(home)

    if not monday_url:
        print("ERROR: could not find Monday/Sales post.", file=sys.stderr)
        return 1

    print(f"Base Flyer/Sales Post: {monday_url}")
    monday_html = fetch(scraper, monday_url)
    monday_range = ranges.get("monday") or range_from_html(monday_html)
    monday_items = extract_items(monday_html, monday_url)

    if not monday_items:
        print("ERROR: zero images extracted from base sales post; refusing to deploy.", file=sys.stderr)
        return 1

    store_location = extract_location(monday_html)

    weekend_range = ranges.get("weekend")
    is_weekend_mode = bool(weekend_url)
    if is_weekend_mode and weekend_over(weekend_range, weekend_forced, today):
        print(f"Weekend post ended {weekend_range[1]}; using the Monday post instead.")
        is_weekend_mode = False
    weekend_items = []
    exclusive = []
    exclusive_urls = set()

    if is_weekend_mode:
        print(f"Weekend Update: {weekend_url}")
        time.sleep(random.uniform(1, 3))
        weekend_html = fetch(scraper, weekend_url)
        weekend_range = weekend_range or range_from_html(weekend_html)
        if weekend_over(weekend_range, weekend_forced, today):
            print(f"Weekend post ended {weekend_range[1]}; using the Monday post instead.")
            weekend_items = []
        else:
            weekend_items = extract_items(weekend_html, weekend_url)
        
        if weekend_items:
            store_location = extract_location(weekend_html) or store_location
            exclusive = find_exclusive_userscript(weekend_items, monday_items) if MATCH_MODE == "userscript" else find_exclusive(weekend_items, monday_items)
            exclusive_urls = {i.url for i in exclusive}
        else:
            is_weekend_mode = False

    target_items = weekend_items if is_weekend_mode else monday_items
    target_url = weekend_url if is_weekend_mode else monday_url
    mode = "weekend" if is_weekend_mode else "flyer"
    valid_range = weekend_range if is_weekend_mode else monday_range
    stale = (not is_weekend_mode) and bool(monday_range) and monday_range[1] < today

    prev_state, state_ok = load_previous_state(scraper)

    seen_ids = previous_ids(prev_state, mode, target_url)
    ids = sorted({item_ident(i) for i in target_items})
    history = build_history(prev_state, mode, target_url, ids)
    price_db = update_price_db(scraper, prev_state, target_items, valid_range)

    html_content = render_html(target_items, monday_url, weekend_url or monday_url, store_location,
                               is_weekend_mode, exclusive_urls, seen_ids, valid_range, stale)

    (OUTPUT_DIR / "index.html").write_text(html_content, encoding="utf-8")

    # Archiving per post
    slug = Path(urlparse(target_url).path).stem or "latest"
    post_archive_dir = ARCHIVE_DIR / slug
    post_archive_dir.mkdir(parents=True, exist_ok=True)
    (post_archive_dir / "index.html").write_text(html_content, encoding="utf-8")
    
    (OUTPUT_DIR / "state.json").write_text(json.dumps({
        "weekend_url": weekend_url if is_weekend_mode else None,
        "flyer_url": monday_url,
        "mode": mode,
        "valid": [valid_range[0].isoformat(), valid_range[1].isoformat()] if valid_range else None,
        "total_count": len(target_items),
        "exclusive_count": len(exclusive),
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "history": history,
        "price_db": price_db,
    }, indent=2), encoding="utf-8")

    maybe_notify(prev_state, state_ok, target_url, is_weekend_mode, target_items, exclusive)
    return 0

if __name__ == "__main__":
    sys.exit(main())
