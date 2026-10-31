#!/usr/bin/env python3
"""
Scrape CocoWest.ca for Costco posts with scored category tagging, smart date-aware
post discovery, Vancouver timestamps, Cart + Save-for-Next-Week shopping list,
custom/historical item search, compact no-image checklist mode, BC tax estimator,
all-time-low badges, and embedded price history.
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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import cloudscraper
from bs4 import BeautifulSoup

BASE_URL = "https://www.cocowest.ca/"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "public"))
DEBUG = os.getenv("DEBUG") == "1"

_owner, _, _repo = os.getenv("GITHUB_REPOSITORY", "").partition("/")
SITE_URL = os.getenv("SITE_URL") or (
    f"https://{_owner.lower()}.github.io/{_repo}/" if _owner and _repo else ""
)
if SITE_URL and not SITE_URL.endswith("/"):
    SITE_URL += "/"
STATE_URL = SITE_URL + "state.json" if SITE_URL else ""

WEEKEND_RE = re.compile(
    os.getenv("WEEKEND_RE", r"weekend[-_\s]*(?:sales[-_\s]*)?update"), re.I
)
IGNORE_RE = re.compile(
    r"(seasonal|electronics|food[-_\s]review|review|recall|contest|clothing|executive|black[-_\s]friday|boxing[-_\s]day)",
    re.I,
)
SALES_POST_RE = re.compile(r"(costco[-_\s]+flyer|sale[-_\s]+items)", re.I)

FUZZY_THRESHOLD = 0.92

CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "attention required",
    "enable javascript and cookies",
)

SKIP_PATH_PREFIXES = (
    "/category/", "/tag/", "/page/", "/author/", "/wp-", "/feed",
    "/comments", "/search", "/about", "/contact", "/privacy",
    "-review/", "/register", "/terms", "/faq", "/disclaimer",
)


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
                url,
                timeout=45,
                headers={"Accept-Language": "en-CA,en;q=0.9", "Referer": BASE_URL},
            )
            if r.status_code == 200 and not looks_like_challenge(r.text):
                return r.text
            last = f"HTTP {r.status_code}, challenge={looks_like_challenge(r.text)}"
        except Exception as exc:
            last = repr(exc)
        wait = 2**attempt + random.random() * 2
        print(
            f"[fetch] {url} attempt {attempt}/{retries} failed ({last}); sleeping {wait:.1f}s",
            file=sys.stderr,
        )
        time.sleep(wait)
    raise RuntimeError(f"Could not fetch {url}: {last}")


def is_post_link(href: str) -> bool:
    u = urlparse(href)
    host = u.netloc.lower().removeprefix("www.")
    if host != urlparse(BASE_URL).netloc.lower().removeprefix("www."):
        return False
    path = u.path
    if not path.strip("/") or path.lower().startswith(SKIP_PATH_PREFIXES):
        return False
    return not re.search(r"\.(jpe?g|png|gif|webp|pdf|xml)$", path, re.I)


_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_ALIASES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_PAT = "|".join(
    _MONTH_NAMES + tuple(sorted(_MONTH_ALIASES, key=len, reverse=True))
)
RANGE_RE = re.compile(
    rf"\b({_MONTH_PAT})\.?\s+(\d{{1,2}})(?:\s*[-–—]\s*|\s+)(?:({_MONTH_PAT})\.?\s+)?(\d{{1,2}})[,\s]+(\d{{4}})",
    re.I,
)


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


def range_from_soup(soup: BeautifulSoup):
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


def sunday_on_or_after(d):
    return d + timedelta(days=(6 - d.weekday()) % 7)


def published_date_from_soup(soup: BeautifulSoup):
    candidates = []
    for prop in ("article:published_time", "og:article:published_time"):
        meta = soup.find("meta", property=prop)
        if meta and meta.get("content"):
            candidates.append(meta["content"])
    art_time = soup.select_one("article time[datetime]")
    if art_time:
        candidates.append(art_time["datetime"])
    for c in candidates:
        try:
            dt = datetime.fromisoformat(c.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/Vancouver")).date()
    return None


def reconcile_range(title_range, pub_date):
    if pub_date is None:
        return title_range
    if title_range and title_range[1] >= pub_date:
        return title_range
    return (pub_date, sunday_on_or_after(pub_date))


def weekend_over(rng, forced: bool, today, monday_range=None) -> bool:
    if forced or not rng:
        return False
    if rng[1] >= today:
        return False
    if monday_range and monday_range[0] > rng[1]:
        return True
    return today > rng[1] + timedelta(days=1)


def discover_posts(home_html: str):
    soup = BeautifulSoup(home_html, "html.parser")
    texts = {}
    order = []
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"]).split("#")[0]
        if not is_post_link(href):
            continue
        text = " ".join(
            [a.get_text(" ", strip=True), a.get("title", ""), a.get("aria-label", "")]
        ).strip()
        if href not in texts:
            texts[href] = text
            order.append(href)
        elif len(text) > len(texts[href]):
            texts[href] = text

    all_posts = [(h, f"{texts[h]} {urlparse(h).path.replace('-', ' ')}") for h in order]
    weekend_env = os.getenv("WEEKEND_URL")
    monday_env = os.getenv("MONDAY_URL")

    entries = [
        {"href": h, "blob": b, "order": n, "range": parse_range(b)}
        for n, (h, b) in enumerate(all_posts)
    ]

    def newest(cands):
        dated = [c for c in cands if c["range"]]
        if dated:
            return max(dated, key=lambda c: (c["range"][1], -c["order"]))
        return cands[0] if cands else None

    weekend_pick = newest([e for e in entries if WEEKEND_RE.search(e["blob"])])
    weekend_url = weekend_env or (weekend_pick["href"] if weekend_pick else None)
    weekend_range = weekend_pick["range"] if weekend_pick else None

    def is_candidate(e):
        return (
            e["href"] != weekend_url
            and not WEEKEND_RE.search(e["blob"])
            and not IGNORE_RE.search(e["blob"])
        )

    monday_pick = newest(
        [
            e
            for e in entries
            if is_candidate(e) and e["range"] and SALES_POST_RE.search(e["blob"])
        ]
    )
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
    re.I,
)
KNOWN_LAZY_ATTRS = (
    "data-src", "data-lazy-src", "data-original", "data-orig-file",
    "data-large-file", "data-full-url", "data-lazy", "data-img-src",
)
SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset", "srcset")
CONTAINER_SELECTORS = (
    "article .entry-content", ".entry-content", ".post-content",
    ".single-content", "article", "main", "#content",
)

BAD_FILENAME_RE = re.compile(
    r"(logo|banner|avatar|gravatar|icon|sprite|emoji|placeholder|spinner|loader|"
    r"button|badge|social|facebook|twitter|instagram|pinterest|youtube|tiktok|"
    r"advert|sponsor|cropped-|blank|pixel|1x1|spacer|newsletter|subscribe|"
    r"next-week|what-is-on-sale|sale-next-week|promo-header)",
    re.I,
)
BAD_HOSTS = (
    "gravatar.com", "s.w.org", "doubleclick.net", "googlesyndication.com",
    "facebook.com", "facebook.net", "google-analytics.com",
)
BAD_CLASS_RE = re.compile(
    r"(sidebar|widget|comment|related|share|social|author|menu|logo|banner|"
    r"(^|[-_ ])(ads?|advert\w*|sponsor\w*)($|[-_ ])|newsletter|subscribe|breadcrumb)",
    re.I,
)
GENERIC_ALT_RE = re.compile(
    r"^(img|image|photo|picture|screenshot|dsc|pxl|p|untitled)?[\s_-]*\d*$", re.I
)
GENERIC_NAME_RE = re.compile(
    r"^(img|image|photo|dsc|pxl|screenshot|untitled|p)?\d*$", re.I
)
FILE_SUFFIX_RE = re.compile(
    r"(?:-\d{2,4}x\d{2,4}|-(?:scaled|rotated|edited|copy|min|large|medium|thumbnail)|[-_]\d{1,2})+$",
    re.I,
)
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

ALT_ITEM_NO_RE = re.compile(r"^\s*(\d{5,8})\b")
FILE_ITEM_NO_RE = re.compile(r"^(\d{5,8})[-_]")

PRICE_RE = re.compile(r"\$\s?(\d[\d,]*)\.(\d{2})\b")
PAREN_RE = re.compile(r"\([^)]*\)")
END_RE = re.compile(
    r"(?:EXPIRES|ENDS|VALID\s+(?:UNTIL|THROUGH|THRU))\s+(?:ON\s+)?(\d{4}-\d{2}-\d{2})",
    re.I,
)

MONEY = r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)"
SAVE_RE = re.compile(
    r"\b(?:you\s+)?(?:instant\s+)?(?:save|savings?)\s*(?:of\s*)?:?\s*" + MONEY, re.I
)
OFF_RE = re.compile(
    MONEY + r"\s*(?:off|instant\s+savings?|savings?|discount)\b", re.I
)
REG_RE = re.compile(
    r"\b(?:reg(?:ular)?|was|orig(?:inal)?)\.?\s*(?:price)?\s*:?\s*" + MONEY, re.I
)
PCT_OFF_RE = re.compile(r"\b(\d{1,2})\s*%\s*off\b", re.I)
FLYER_RE = re.compile(r"costco\s*1", re.I)

CATEGORY_OVERRIDES = (
    (re.compile(r"\b(sound\s*bars?|apple\s*watch(?:es)?|apple\s*tvs?|apple\s*airpods?|apple\s*ipads?|apple\s*macbooks?|apple\s*pencils?|surge\s*protectors?|power\s*banks?|dash\s*cams?|memory\s*cards?|sd\s*cards?|micro\s*sd|gift\s*cards?|gaming\s*monitors?|smart\s*tvs?|oled\s*tvs?|qled\s*tvs?)\b", re.I), "electronics"),
    (re.compile(r"\b(water\s*flossers?|waterpiks?|body\s*butters?|body\s*wash(?:es)?|hand\s*soaps?|hand\s*creams?|face\s*creams?|eye\s*creams?|shave\s*creams?|shaving\s*creams?|lip\s*balms?|tea\s*tree|epsom\s*salts?|milk\s*thistle|protein\s*powders?|whey\s*protein|collagen\s*peptides?|electrolyte\s*powders?|liquid\s*i\.?v\.?|contact\s*lens(?:es)?|heating\s*pads?| massage\s*guns?)\b", re.I), "health"),
    (re.compile(r"\b(motor\s*oils?|synthetic\s*oils?|windshield\s*wash|wiper\s*blades?|cat\s*litter|dog\s*food|cat\s*food|dog\s*treats?|cat\s*treats?|pee\s*pads?|dish\s*soaps?|laundry\s*detergents?|dishwasher\s*tabs?|dishwasher\s*pods?|dryer\s*sheets?|garbage\s*bags?|trash\s*bags?|food\s*wrap|parchment\s*paper|aluminum\s*foil|furnace\s*filters?|toilet\s*paper|paper\s*towels?)\b", re.I), "household"),
    (re.compile(r"\b(bar\s*stools?|towel\s*bars?|towel\s*warmers?|tea\s*towels?|bath\s*towels?|bath\s*mats?|welcome\s*mats?|area\s*rugs?|bed\s*frames?|picture\s*frames?|sleeping\s*bags?|duffel\s*bags?|garment\s*bags?|luggage|step\s*stools?|folding\s*tables?|folding\s*chairs?|office\s*chairs?|gaming\s*chairs?|patio\s*umbrellas?|fire\s*pits?|water\s*bottles?|travel\s*mugs?)\b", re.I), "home"),
    (re.compile(r"\b(water\s*pitchers?|brita|air\s*purifiers?|water\s*coolers?|ice\s*makers?|coffee\s*makers?|espresso\s*machines?|rice\s*cookers?|slow\s*cookers?|pressure\s*cookers?|air\s*fryers?|stand\s*mixers?|hand\s*mixers?|immersion\s*blenders?|food\s*processors?|food\s*savers?|vacuum\s*sealers?|bread\s*makers?|waffle\s*makers?)\b", re.I), "appliances"),
)

CATEGORY_RULES = {
    "groceries": re.compile(
        r"\b(coffee|espresso\s*beans|k-?cup|nescafe|starbucks|tim\s*hortons|lavazza|kicking\s*horse|zavida|tea|matcha|chai|kombucha|"
        r"snack|meat|chicken|beef|pork|salmon|shrimp|prawn|halibut|cod|tilapia|trout|tuna|Pollock|haddock|basa|sole|lobster|crab|scallop|mussel|clam|oyster|calamari|octopus|sushi|poke|"
        r"bacon|sausage|wiener|hot\s*dog|pepperoni|salami|prosciutto|pancetta|chorizo|mortadella|bresaola|capicola|ham|turkey|duck|lamb|veal|bison|venison|deli|roast|steak|striploin|ribeye|sirloin|tenderloin|flank|tomahawk|prime\s*rib|brisket|ribs|wings|drumstick|thigh|breast|ground\s*beef|ground\s*turkey|ground\s*pork|meatball|burger|patty|patties|kebab|souvlaki|gyro|schnitzel|carnitas|pulled\s*pork|"
        r"juice|lemonade|limeade|smoothie|nectar|cider|milk|oat\s*milk|almond\s*milk|soy\s*milk|coconut\s*milk|fairlife|lactantia|natrel|dairyland|silk|earth'?s\s*own|cream\s*cheese|sour\s*cream|whipping\s*cream|creamer|half\s*&\s*half|butter|ghee|margarine|becel|egg|egg\s*whites|yogurt|greek\s*yogurt|skyr|oikos|danone|activia|iogo|liberte|siggi|kefir|yakult|"
        r"cheese|cheddar|mozzarella|parmesan|parmigiano|grana\s*padano|pecorino|brie|camembert|gouda|edam|feta|havarti|provolone|swiss|gruyere|emmental|jarlsberg|manchego|ricotta|mascarpone|cotija|halloumi|paneer|queso|burrata|bocconcini|cottage\s*cheese|string\s*cheese|チーズ|babybel|laughing\s*cow|boursin|philadelphia|balderson|black\s*diamond|cracker\s*barrel|saputo|tre\s*stelle|bothwell|oka|"
        r"chips|crisps|doritos|lays|ruffles|pringles|cheetos|tostitos|sun\s*chips|takis|kettle\s*brand|miss\s*vickie|hardbite|terra|popcorners|smartfood|popcorn|boomchickapop|skinny\s*pop|pretzel|cracker|ritz|triscuit|wheat\s*thin|breton|carr'?s|mary'?s\s*gone|goldfish|cheez-?it|rice\s*cake|rice\s*cracker|seaweed|nori|jerky|jack\s*link|oberto|korean\s*bbq\s*pork|biltong|pork\s*rind|pepperoni\s*stick|grimms|freybe|schneiders|maple\s*leaf|pillers|"
        r"chocolate|lindt|lindor|ferrero|rocher|ghirardelli|godiva|guylian|turtles|toblerone|kit\s*kat|reeses|snickers|mars|twix|aero|coffee\s*crisp|smarties|m&m|hershey|cadbury|milka|kinder|nutella|brookside|barkthins|chocxo|Jojo|hu\b|gummy|gummies|haribo|maynards|sour\s*patch|swedish\s*fish|skittles|starburst|welch'?s\s*fruit|fruit\s*snack|fruit\s*leather|sunrype|bear\s*paws|wagon\s*wheels|candy|licorice|twizzlers|caramel|werther|toffee|fudge|marshmallow|nougat|halva|baklava|pocky|pepero|hi-?chew|mentos|tic\s*tac|excel|trident|gum|"
        r"cookie|oreo|chips\s*ahoy|peek\s*freans|celebration|leclerc|walker'?s|lotus|biscoff|tate'?s|nonni|biscotti|shortbread|palmier|madeleine|macaron|stroopwafel|wafer|pirouette|pizzelle|brownie|blondie|muffin|croissant|pain\s*au\s*chocolat|danish|scone|strudel|turnovers?|cinnamon\s*roll|cinnamon\s*bun|bagel|brioche|bread|loaf|sourdough|ciabatta|baguette|artisan\s*bun|kaiser|hot\s*dog\s*bun|hamburger\s*bun|flatbread|naan|pita|tortilla|wrap|lavash|english\s*muffin|pancake|krusteaz|waffle|eggo|crepe|pie|cake|cheesecake|tiramisu|mousse|profiterole|eclair|cream\s*puff|tart|pastel\s*de\s*nata|flan|pudding|jell-?o|donut|doughnut|churro|panettone|"
        r"cereal|cheerios|frosted\s*flakes|aisin\s*bran|froot\s*loops|krave|special\s*k|cinnamon\s*toast|honey\s*bunches|shreddies|mini-?wheats|vector|magic\s*spoon|catalina\s*crunch|granola|oats|oatmeal|quaker|one\s*degree|bob'?s\s*red\s*mill|porridge|muesli|granola\s*bar|protein\s*bar|pure\s*protein|clif|lara\s*bar|kind\s*bar|rxbar|built\s*bar|kirkland\s*protein\s*bar|made\s*good|nature\s*valley|nutri-?grain|kashi|cliff|fibre\s*1|belvita|"
        r"almond|cashew|pistachio|walnut|pecan|macadamia|hazelnut|brazil\s*nut|pine\s*nut|peanut|marcona|mixed\s*nuts|trail\s*mix|pub\s*mix|sesame\s*stick|chestnut|pumpkin\s*seed|pepita|sunflower\s*seed|chia|flax|hemp\s*heart|raisin|cranberry|craisin|date|medjool|fig|prune|apricot|dried\s*mango|dried\s*fruit|freeze\s*dried|"
        r"rice|jasmine|basmati|arborio|calrose|wild\s*rice|sticky\s*rice|minute\s*rice|ben'?s\s*original|bibigo\s*rice|quinoa|couscous|bulgur|farro|barley|lentil|daal|dal|chickpea|garbanzo|beans?|black\s*bean|kidney\s*bean|pinto\s*bean|refried|baked\s*beans|pasta|spaghetti|penne|rigatoni|fusilli|farfalle|macaroni|kd\b|kraft\s*dinner|annie'?s|barilla|garofalo|raos|fettuccine|linguine|lasagna|ravioli|tortellini|gnocchi|orzo|olivieri|noodle|ramen|shin\s*ramyun|buldak|nongshim|indomie|ichiban|jin\s*ramen|udon|soba|pho|rice\s*stick|vermicelli|pad\s*thai|chow\s*mein|lo\s*mein|japchae|"
        r"sauce|marinara|pasta\s*sauce|pesto|alfredo|bolognese|vodka\s*sauce|salsa|pace|tostitos\s*salsa|guacamole|wholly|hummus|sabra|fontaine\s*sante|tzatziki|baba\s*ghanoush|bitchin|dip|queso\s*dip|spinach\s*dip|ketchup|french'?s|heinz|mustard|dijon|maille|mayo|mayonnaise|hellmann|kewpie|miracle\s*whip|aioli|relish|tartar|bbq\s*sauce|sweet\s*baby\s*ray|bull'?s-?eye|teriyaki|bachan|kikkoman|soy\s*sauce|tamari|fish\s*sauce|oyster\s*sauce|hoisin|plum\s*sauce|sweet\s*chili|sriracha|tabasco|frank'?s\s*redhot|cholula|valentina|chili\s*oil|lao\s*gan\s*ma|gochujang|miso|curry\s*paste|worcestershire|vinegar|balsamic|apple\s*cider\s*vinegar|mirin|olive\s*oil|avocado\s*oil|canola\s*oil|vegetable\s*oil|peanut\s*oil|coconut\s*oil|sesame\s*oil|grapeseed\s*oil|truffle\s*oil|chosen\s*foods|filippo\s*berio|terra\s*delyssa|pam\b|cooking\s*spray|dressing|ranch|caesar|balsamic\s*vinaigrette|briannas|renee'?s|kraft\s*dressing|marinade|tzatziki|"
        r"soup|campbell|progresso|amy'?s|haburant|sprague|broth|bone\s*broth|stock|better\s*than\s*bouillon|bouillon|oxtail|french\s*onion|clam\s*chowder|lobster\s*bisque|tomato\s*soup|chicken\s*noodle|miso\s*soup|chili|stew|curry|tikka\s*masala|butter\s*chicken|korma|vindaloo|madras|chana\s*masala|chowder|"
        r"flour|all-?purpose|almond\s*flour|coconut\s*flour|robin\s*hood|king\s*arthur|sugar|brown\s*sugar|icing\s*sugar|redpath|sweetener|splenda|stevia|monk\s*fruit|erythritol|swerve|lakanto|xylitol|honey|manuka|maple\s*syrup|agave|molasses|corn\s*syrup|jam|jelly|marmalade|preserve|bonne\s*maman|smucker|peanut\s*butter|skippy|jif|kraft\s*peanut|almond\s*butter|sunbutter|tahini|cocoa|chocolate\s*chips|hershey\s*chipits|callebaut|baking\s*powder|baking\s*soda|yeast|fleischmann|cornstarch|gelatin|vanilla\s*extract|spice|seasoning|club\s*house|mccormick|lawry|old\s*bay|tajin|everything\s*bagel|montreal\s*steak|garlic\s*powder|onion\s*powder|cinnamon|paprika|cumin|oregano|turmeric\s*powder|chili\s*powder|curry\s*powder|italian\s*seasoning|herbs?\s*de\s*provence|bay\s*leaves|saffron|peppercorn|black\s*pepper|sea\s*salt|pink\s*salt|himalayan\s*salt|kosher\s*salt|"
        r"fruit|apple|honeycrisp|gala|fuji|ambrosia|granny\s*smith|pink\s*lady|banana|plantain|orange|navel|cara\s*cara|mandarin|clementine|tangerine|sumo\s*citrus|grapefruit|pomelo|lemon|lime|berry|berries|strawberry|blueberry|raspberry|blackberry|cranberries|grape|cotton\s*candy\s*grape|cherry|cherries|peach|nectarine|apricot|pear|bartlett|anjou|bosc|plum|mango|ataulfo|pineapple|melon|cantaloupe|honeydew|watermelon|kiwi|zespri|papaya|dragon\s*fruit|pitaya|passion\s*fruit|pomegranate|persimmon|lychee|longan|rambutan|guava|fig|starfruit|coconut|avocado|guac|tomato|grape\s*tomato|cherry\s*tomato|roma\s*tomato|heirloom|"
        r"vegetable|veggie|salad|taylor\s*farms|earthbound|mann'?s|lettuce|romaine|iceberg|butter\s*lettuce|spring\s*mix|spinach|kale|arugula|chard|bok\s*choy|napa\s*cabbage|cabbage|coleslaw|broccoli|broccolini|rapini|cauliflower|riced\s*cauliflower|asparagus|brussels\s*sprout|green\s*bean|french\s*bean|snap\s*pea|snow\s*pea|peas|edamame|celery|carrot|baby\s*carrot|cucumber|english\s*cucumber|mini\s*cucumber|zucchini|squash|butternut|spaghetti\s*squash|pumpkin|eggplant|aubergine|bell\s*pepper|sweet\s*pepper|jalapeno|habanero|poblano|mushroom|cremini|portobello|shiitake|oyster\s*mushroom|enoki|chanterelle|onion|red\s*onion|yellow\s*onion|white\s*onion|sweet\s*onion|vidalia|shallot|scallion|green\s*onion|leek|garlic|peeled\s*garlic|ginger|potato|russet|yukon\s*gold|red\s*potato|fingerling|mini\s*potato|little\s*potato|sweet\s*potato|yam|fries|french\s*fries|cavendish|mccain|hash\s*brown|tater\s*tot|waffle\s*fries|onion\s*ring|corn|sweet\s*corn|corn\s*on\s*the\s*cob|artichoke|hearts?\s*of\s*palm|bamboo\s*shoot|water\s*chestnut|beet|beetroot|radish|daikon|turnip|parsnip|rutabaga|fennel|okra|sprouts|alfalfa|bean\s*sprout|tofu|tempeh|seitan|beyond\s*meat|impossible\s*burger|morningstar|gardein|field\s*roast|lightlife|kimchi|jongga|sauerkraut|pickle|vlasic|bick'?s|moishe|cornichon|gherkins?|olive|kalamata|castelvetrano|stuffed\s*olive|caper|sundried\s*tomato|roasted\s*red\s*pepper|peppadew|"
        r"pizza|frozen\s*pizza|motor\s*city|sabatasso|caulipower|dr\.\s*oetker|delissio|di\s*giorno|kirkland\s*pizza|pizza\s*crust|garlic\s*bread|garlic\s*fingers|dumpling|mandu|bibigo|gyoza|siwin|summ!|wonton|potsticker|xiao\s*long\s*bao|soup\s*dumpling|har\s*gow|siu\s*mai|dim\s*sum|bao\b|bun|spring\s*roll|egg\s*roll|lumpia|samosa|pakora|spanakopita|tiropita|quiche|frittata|omelette|egg\s*bite|starbucks\s*egg|burrito|chimichanga|enchilada|quesadilla|taquito|flauta|el\s*monterey|taco|taco\s*kit|empanada|tamale|pupusa|arepa|perogy|pierogi|pelmeni|cheemo|cabbage\s*roll|poutine|chicken\s*nugget|chicken\s*strip|chicken\s*tender|chicken\s*finger|chicken\s*chunk|chicken\s*slider|chicken\s*pot\s*pie|shepherd'?s\s*pie|lasagna|mac\s*&\s*cheese|mac\s*and\s*cheese|stuffed\s*pepper|cordon\s*bleu|kiev|fish\s*stick|fish\s*fillet|high\s*liner|jane'?s|hampton\s*house|pinty'?s|eri\b|maple\s*lodge|zabiha\s*halal|mina\s*halal|saffron\s*road|sukhi'?s|royal\s*asia|ajinomoto|cj\b|pulmuone|"
        r"ice\s*cream|gelato|sorbet|sherbet|frozen\s*yogurt|froyo|popsicle|ice\s*pop|freezie|haagen-?dazs|ben\s*&\s*jerry|breyers|chapman'?s|kawarthas?|island\s*farms|magnum|drumstick|klondike|good\s*humor|melona|mochi|yasso|halo\s*top|nicks|talenti|"
        r"soda|pop\b|coke|coca-?cola|diet\s*coke|coke\s*zero|pepsi|sprite|7up|mountain\s*dew|ginger\s*ale|canada\s*dry|schweppes|root\s*beer|a&w|mug\b|barq|dr\s*pepper|fanta|orangina|san\s*pellegrino|pellegrino|perrier|bubly|aha\b|la\s*croix|spindrift|waterloo|zevia|olipop|poppi|sparkling\s*ice|talking\s*rain|sparkling\s*water|mineral\s*water|spring\s*water|purified\s*water|distilled\s*water|evian|fiji|voss|smartwater|dasani|aquafina|eska|nestle\s*pure\s*life|kirkland\s*water|alkaline\s*water|coconut\s*water|vita\s*coco|harmless\s*harvest|thirsty\s*buddha|aloe\s*vera\s*drink|iced\s*tea|brisk|nestea|pure\s*leaf|gold\s*peak|arizona|snapple|red\s*bull|monster\s*energy|rockstar|celsius|alani\s*nu|c4\s*energy|ghost\s*energy|reign|nos\b|guru|gatorade|powerade|bodyarmor|prime\s*hydration|biosteel\s*drink|vitaminwater|propel|mio\b|crystal\s*light|tang|kool-?aid|country\s*time|good\s*host|nesquik|ovaltine|carnations?\s*hot\s*chocolate|swiss\s*miss|van\s*houtte|mother\s*parkers|folgers|maxwell\s*house|nabob|ethical\s*bean|salt\s*spring\s*coffee|level\s*ground|canterbury|balzac|illy|segafredo|davidoff|tassimo|dolce\s*gusto|tetley|twinings|tazo|stash|celestial|bigelow|yogi|traditional\s*medicinals|david'?s\s*tea|red\s*rose|ito\s*en|matcha\s*love|sencha|hojicha|oolong|jasmine\s*tea|earl\s*grey|english\s*breakfast|chamomile|peppermint\s*tea|green\s*tea|black\s*tea|herbal\s*tea|torani|monin|baileys\s*creamer|international\s*delight|coffee-?mate|silk\s*creamer|"
        r"wine|beer|lager|ale\b|ipa\b|stout|pilsner|cider|cooler|seltzer|white\s*claw|truly|nutrl|smirnoff|bacardi|captain\s*morgan|crown\s*royal|jack\s*daniel|jameson|johnnie\s*walker|glenfiddich|glenlivet|macallan|maker'?s\s*mark|woodford|bulleit|jim\s*beam|grey\s*goose|belvedere|tito'?s|absolut|ketel\s*one|kirkland\s*vodka|tanqueray|bombay\s*sapphire|hendrick'?s|empress\s*gin|patron|casamigos|don\s*julio|1800\s*tequila|jose\s*cuervo|hennessy|remy\s*martin|courvoisier|baileys|kahlua|cointreau|grand\s*marnier|aperol|campari|jagermeister|amaretto|disaronno|cabernet|sauvignon|merlot|pinot\s*noir|shiraz|syrah|malbec|zinfandel|chardonnay|pinot\s*grigio|pinot\s*gris|riesling|moscato|rose\b|prosecco|champagne|cava|cremant|sangria|sake|soju)(?:s|es)?\b",
        re.I,
    ),
    "appliances": re.compile(
        r"\b(toaster|toaster\s*oven|blender|immersion\s*blender|vitamix|nutribullet|magic\s*bullet|ninja|foodi|creami|slushi|woodfire|espresso|keurig|nespresso|vertuo|breville|barista|bambino|delonghi|magnifica|dinamica|jura|saeco|philips\s*lattego|moccamaster|cuisinart|kitchenaid|smeg|hamilton\s*beach|black\+decker|ostba|braun\s*brew|"
        r"air\s*fryer|gourmia|cosori|instant\s*vortex|kalorik|chefman|dash|microwave|panasonic\s*inverter|toshiba\s*microwave|stand\s*mixer|hand\s*mixer|cookware|pan|skillet|fry\s*pan|frying\s*pan|saute\s*pan|saucepan|stockpot|dutch\s*oven|braiser|cast\s*iron|le\s*creuset|staub|lodge|griddle|roaster|baking\s*sheet|cookie\s*sheet|bakeware|muffin\s*pan|cake\s*pan|loaf\s*pan|pie\s*plate|casserole|nordic\s*ware|usa\s*pan|henckels|zwilling|miyabi|wusthof|shun|global\s*knives|victorinox|cangshan|tramontina|all-?clad|calphalon|anolon|scanpan|hexclad|greenpan|caraway|t-?fal|tefal|lagostina|paderno|circulon|meyer|farberware|rock\s*by\s*starfrit|starfrit|oxfords?|oxo\s*good\s*grips|pyrex|corningware|anchor\s*hocking|knives|knife|chef'?s\s*knife|santoku|paring\s*knife|bread\s*knife|steak\s*knives|knife\s*set|knife\s*block|cutting\s*board|butcher\s*block|colander|strainer|mixing\s*bowl|salad\s*spinner|mandoline|grater|zester|peeler|can\s*opener|wine\s*opener|corkscrew|thermometer|meat\s*thermometer|meater|thermapen|kitchen\s*scale|food\s*scale|"
        r"kettle|electric\s*kettle|gooseneck\s*kettle|fellow\s*stagg|tiger|zojirushi|thermos|vacuum|cordless\s*vacuum|stick\s*vacuum|upright\s*vacuum|canister\s*vacuum|wet\s*dry\s*vac|robot\s*vacuum|robovac|dyson|v8\b|v10\b|v11\b|v12\b|v15\b|gen5|ball\s*animal|airwrap|supersonic|corrale|airstrait|shark|stratos|detect\s*pro|navigator|rotator|flexstyle|smoothstyle|roomba|irobot|roborock|ecovacs|deebot|dreame|narwal|eufy\s*clean|tineco|bissell|crosswave|little\s*green|spotclean|hoover|miele|sebo|eureka|dirt\s*devil|shop-?vac|dewalt\s*vac|carpet\s*cleaner|steam\s*mop|steam\s*cleaner|karcher|dupray|"
        r"shaver|electric\s*shaver|foil\s*shaver|rotary\s*shaver|braun\s*series|norelco|oneblade|philips\s*shaver|philips\s*series|panasonic\s*arc|trimmer|beard\s*trimmer|hair\s*clipper|ear\s*&\s*nose|wahl|manscaped|babyliss|conair\s*man|remington|epilator|silk-?epil|ipl\b|ulike|tria|electric\s*toothbrush|sonicare|diamondclean|protectiveclean|oral-?b\s*io|oral-?b\s*pro|waterpik|water\s*flosser|"
        r"fan|tower\s*fan|pedestal\s*fan|circulator|ceiling\s*fan|woozo|iris\s*usa|vornado|lasko|bionaire|rowenta|dyson\s*cool|dyson\s*hot|dyson\s*purifier|heater|space\s*heater|ceramic\s*heater|oil\s*radiator|patio\s*heater|de'longhi\s*heater|air\s*conditioner|window\s*ac|portable\s*ac|mini\s*split|dehumidifier|humidifier|cool\s*mist|warm\s*mist|evaporative|air\s*purifier|hepa\s*filter|winix|levoit|blueair|coway|austin\s*air|iqair|germguardian|honeywell\s*purifier|medify|duux|stadler\s*form|"
        r"grill|gas\s*grill|propane\s*grill|charcoal\s*grill|pellet\s*grill|kamado|flat\s*top|griddle\s*grill|indoor\s*grill|contact\s*grill|panini\s*press|weber|genesis|spirit\s*ii|q1200|q2200|traeger|pit\s*boss|camp\s*chef|recteq|green\s*mountain|blackstone|napoleon|broil\s*king|char-?broil|nexgrill|big\s*green\s*egg|kamado\s*joe|masterbuilt|bradley\s*smoker|smoker|pizza\s*oven|ooni|gozney|roccbox|solo\s*stove\s*pi|"
        r"cooker|rice\s*cooker|slow\s*cooker|pressure\s*cooker|multi-?cooker|instant\s*pot|crock-?pot|cuckoo|aromab|sous\s*vide|anova|joule|"
        r"fridge|refrigerator|mini\s*fridge|bar\s*fridge|wine\s*fridge|wine\s*cellar|wine\s*cooler|beverage\s*center|beverage\s*cooler|kegerator|freezer|chest\s*freezer|upright\s*freezer|dishwasher|washer|washing\s*machine|dryer|laundry\s*center|range|stove|cooktop|induction\s*cooktop|wall\s*oven|range\s*hood|over-?the-?range|garbage\s*disposal|insinkerator|danby|hisense\s*fridge|midea|frigidaire|galanz|arctic\s*king|insignia|whirlpool|maytag|kitchenaid\s*appliance|ge\s*profile|ge\s*appliances|cafe\s*appliance|lg\s*wash|lg\s*fridge|samsung\s*bespoke|bosch\s*dishwasher|marathon\s*appliances| Koolatron|silhouette|"
        r"iron|steam\s*iron|garment\s*steamer|chi\s*iron|sunbeam|conair|hair\s*dryer|blow\s*dryer|straightener|flat\s*iron|curling\s*iron|curling\s*wand|hot\s*air\s*brush|revlon\s*one-?step|drybar|t3\b|ghd\b|bio\s*ionic|l'ange|sodastream|drinkmate|aarke|sparkel|brita\s*pitcher|brita\s*dispenser|zerowater|berkey|water\s*drop|reverse\s*osmosis|water\s*cooler|vitapur|primo\s*water|ice\s*maker|nugget\s*ice|ge\s*opal|euhomy|silonn|juicer|masticating\s*juicer|nama|hurom|omega\s*juicer|breville\s*juicer|food\s*processor|magimix|meat\s*grinder|slicer|dehydrator|excalibur|cosori\s*dehydrator|vacuum\s*sealer|foodsaver|nesco|anova\s*sealer|waffle\s*maker|pancake\s*maker|crepe\s*maker|bread\s*maker|pasta\s*maker|marcato|philips\s*pasta|popcorn\s*maker|whirley\s*pop|fondue|raclette|hot\s*pot|electric\s*skillet|electric\s*wok|electric\s*griddle|presto|zojirushi\s*griddle)(?:s|es)?\b",
        re.I,
    ),
    "electronics": re.compile(
        r"\b(tv|television|smart\s*tv|google\s*tv|fire\s*tv|roku\s*tv|tizen|webos|oled|qd-?oled|qled|neo\s*qled|qned|nanocell|uhd|crystal\s*uhd|4k|8k|1080p|1440p|2160p|mini-?led|micro-?led|dolby\s*vision|hdr10|bravia|the\s*frame|the\s*serif|lg\s*c[3456]|lg\s*g[3456]|lg\s*b[3456]|tcl\s*q\d|hisense\s*u[678]|vizio|"
        r"monitor|gaming\s*monitor|curved\s*monitor|ultrawide|freesync|g-?sync|ips\s*panel|va\s*panel|odyssey\s*g\d|ultrasharp|proart|benq|viewsonic|aoc|msi\s*optix|msi\s*mag|lg\s*ultragear|lg\s*ultrabook|lg\s*gram|alienware|"
        r"speaker|bluetooth\s*speaker|party\s*speaker|bookshelf\s*speaker|smart\s*speaker|echo\s*dot|echo\s*show|homepod|nest\s*audio|nest\s*mini|headphone|earbud|earphone|airpods?|airpods?\s*pro|airpods?\s*max|galaxy\s*buds|pixel\s*buds|quietcomfort|qc45|qc\s*ultra|wh-?1000xm[456]|wf-?1000xm[45]|linkbuds|soundcore|liberty\s*4|space\s*a40|sleep\s*a20|openrun|shokz|bone\s*conduction|soundbar|sound\s*bar|subwoofer|home\s*theater|av\s*receiver|denon|yamaha\s*audio|marantz|onkyo|polk\s*audio|klipsch|sonos|arc\b|beam\b|era\s*100|era\s*300|sub\s*mini|roam\b|move\s*2|bose|jbl|flip\s*[567]|charge\s*[56]|xtreme\s*[34]|partybox|harman\s*kardon|ultimate\s*ears|wonderboom|megaboom|marshall|edifier|audio-?technica|turntable|record\s*player|victrola|fluance|"
        r"laptop|notebook|ultrabook|chromebook|macbook|macbook\s*air|macbook\s*pro|imac|mac\s*mini|mac\s*studio|mac\s*pro|desktop|gaming\s*pc|all-?in-?one|mini\s*pc|nuc\b|thinkpad|ideapad|legion|yoga\b|vivobook|zenbook|rog\s*zephyrus|rog\s*strix|tuf\s*gaming|inspiron|xps\b|latitude|pavilion|envy\s*x360|spectre|omen\b|victus|aspire|swift\s*go|nitro\s*\d|predator\s*helios|cyborg|katana|stealth\b|raider|surface\s*pro|surface\s*laptop|galaxy\s*book|"
        r"intel\s*core|core\s*i[3579]|core\s*ultra|ryzen\s*[3579]|snapdragon\s*x|apple\s*m[1234]|geforce|rtx\s*[345]0\d0|radeon\s*rx|"
        r"tablet|ipad|ipad\s*air|ipad\s*pro|ipad\s*mini|galaxy\s*tab|lenovo\s*tab|fire\s*hd|kindle|paperwhite|oasis|scribe|kobo|clara|libra|elipsa|e-?reader|boox|remarkable|"
        r"phone|smartphone|cell\s*phone|iphone|galaxy\s*s2[3456]|galaxy\s*z|galaxy\s*a[1235]\d|pixel\s*[789]|pixel\s*10|motorola|moto\s*g|oneplus|nothing\s*phone|unlocked\s*phone|"
        r"charger|wall\s*charger|gan\s*charger|car\s*charger|wireless\s*charger|magsafe|qi2|charging\s*station|cable|usb-?c|lightning\s*cable|thunderbolt|hdmi|displayport|ethernet\s*cable|cat6|adapter|dongle|dock|docking\s*station|usb\s*hub|kvm\s*switch|anker|ugreen|belkin|satechi|caldigit|baseus|spigen|otterbox|mophie|scosche|iottie|"
        r"router|wi-?fi\s*router|mesh\s*wi-?fi|wi-?fi\s*[67]|tri-?band|dual-?band|modem|network\s*switch|range\s*extender|powerline|tp-?link|deco|archer|netgear|nighthawk|orbi|eero|asus\s*rt|asus\s*zenwifi|google\s*nest\s*wifi|ubiquiti|unifi|starlink|"
        r"camera|digital\s*camera|vlog\s*camera|webcam|dslr|mirrorless|canon\s*eos|nikon\s*z|sony\s*alpha|sony\s*zv|fujifilm|instax|polaroid|gopro|hero\s*1[123]|insta360|osmo\s*pocket|osmo\s*action|dji|mini\s*[34]\s*pro|air\s*3|mavic|drone|gimbal|ring\s*light|elgato|cam\s*link|stream\s*deck|dash\s*cam|garmin\s*dash|nextbase|viofo|thinkware|blackvue|redtiger|vantrue|"
        r"security\s*camera|ip\s*camera|poe\s*camera|nvr\b|dvr\b|floodlight\s*cam|trail\s*camera|baby\s*monitor|nanit|owlet|eufy\s*security|solocam|ring\s*doorbell|ring\s*stick\s*up|blink\s*outdoor|blink\s*mini|arlo|reolink|lorex|swann|wyze|tapo|ecobee|nest\s*cam|nest\s*doorbell|nest\s*thermostat|smart\s*thermostat|mysa|sensi|smart\s*lock|schlage\s*encode|yale\s*assure|august\s*lock|aqara|level\s*lock|kasa|govee|philips\s*hue|nanoleaf|lutron\s*caseta|smart\s*plug|smart\s*bulb|smart\s*switch|"
        r"smart\s*watch|smartwatch|fitness\s*tracker|running\s*watch|gps\s*watch|garmin|forerunner|fenix|epix|venu|vivoactive|instinct|lily\b|approach|edge\s*\d+|inreach|fitbit|charge\s*6|versa|inspire|sense\s*2|apple\s*watch|ultra\s*2|series\s*[89]|series\s*10|galaxy\s*watch|pixel\s*watch|amazfit|coros|suunto|whoop|oura\s*ring|airtag|smarttag|tile\s*tracker|chipolo|"
        r"projector|laser\s*projector|short\s*throw|portable\s*projector|xgimi|nebula\s*capsule|epson\s*epiqvision|epson\s*home\s*cinema|benq\s*projector|optoma|samsung\s*freestyle|"
        r"battery|batteries|aa\s*batteries|aaa\s*batteries|9v\b|cr2032|button\s*cell|rechargeable\s*batteries|eneloop|duracell|optimum|energizer|kirkland\s*batteries|ups\b|battery\s*backup|cyberpower|apc\b|eatons?|surge\s*protector|power\s*bar|power\s*strip|power\s*bank|portable\s*charger|power\s*station|ecoflow|delta\s*2|river\s*2|jackery|explorer\s*\d+|bluetti|anker\s*solix|solar\s*panel|"
        r"ssd|solid\s*state|hard\s*drive|external\s*drive|portable\s*ssd|nvme|m\.2\b|sata|flash\s*drive|thumb\s*drive|usb\s*drive|memory\s*card|sd\s*card|microsd|microsdxc|cfexpress|sandisk|extreme\s*pro|wd\b|western\s*digital|my\s*passport|easystore|wd_black|seagate|one\s*touch|ironwolf|exos|crucial|samsung\s*t[79]|samsung\s*9[89]0|lexar|kingston|pny|sabrent|synology|qnap|nas\b|"
        r"fire\s*tv\s*stick|fire\s*stick|roku\s*express|roku\s*streaming|chromecast|google\s*tv\s*streamer|apple\s*tv\s*4k|nvidia\s*shield|nintendo|switch\s*oled|switch\s*2|switch\s*lite|joy-?con|pro\s*controller|mario|zelda|pokemon|playstation|ps5|dualsense|ps\s*portal|psvr2|xbox|series\s*x|series\s*s|meta\s*quest|quest\s*3|steam\s*deck|rog\s*ally|legion\s*go|backbone\s*one|gaming\s*headset|gaming\s*mouse|wireless\s*mouse|ergonomic\s*mouse|mx\s*master|mx\s*anywhere|mx\s*keys|mechanical\s*keyboard|gaming\s*keyboard|mouse\s*pad|desk\s*mat|razer|deathadder|blackwidow|huntsman|kraken|logitech|g\s*pro|g502|lightspeed|corsair|steelseries|arctis|hyperx|cloud\s*ii|turtle\s*beach|astro\s*a\d+|keychron|epomaker|thrustmaster|moza|fanatec|"
        r"printer|all-?in-?one\s*printer|inkjet|laser\s*printer|tank\s*printer|ecotank|megatank|smart\s*tank|label\s*maker|dymo|brother\s*p-?touch|scanner|shredder|fellowes|royal\s*sov|epson|brother|hp\s*envy|hp\s*officejet|hp\s*laserjet|canon\s*pixma|canon\s*imageclass|ink\s*cartridge|toner|hp\s*6[357]xl|hp\s*9[0156]\d|cricut|silhouette\s*cameo|"
        r"binoculars|telescope|celestron|nikon\s*prostaff|bushnell|rangefinder|walkie\s*talkie|two-?way\s*radio|midland|cobra\s*radio|uniden|metal\s*detector|weather\s*station|acurite|la\s*crosse|digital\s*frame|aura\s*frame|skylight\s*frame|nixplay)(?:s|es)?\b",
        re.I,
    ),
    "household": re.compile(
        r"\b(paper\s*towel|bounty|sponge\s*towel|kirkland\s*paper\s*towel|toilet\s*paper|bath\s*tissue|charmin|cashmere|royale|scott|purex\s*bath|kirkland\s*bath\s*tissue|facial\s*tissue|kleenex|scotties|puffs|napkins?|vanity\s*fair|wet\s*wipes|flushable\s*wipes|cottonelle|dude\s*wipes|"
        r"detergent|laundry\s*detergent|laundry\s*pods|laundry\s*pac|tide|pods\b|power\s*pods|gain|flings|persil|purex|all\s*free\s*clear|arm\s*&\s*hammer|ecos\b|nellie'?s|tru\s*earth|woolite|zero\s* detergent|downy|unstopables|rinse\s*&\s*refresh|bounce|dryer\s*sheet|fabric\s*softener|scent\s*booster|snuggle|fleecy|wool\s*dryer\s*ball|oxiclean|resolve|shout\b|spray\s*'n\s*wash|tide\s*to\s*go|stain\s*remover|bleach|clorox|javex|borax|washing\s*soda|"
        r"dish\s*soap|dish\s*liquid|dawn|powerwash|platinum\s*plus|palmolive|ivory\s*dish|ecos\s*dish|meyers\s*clean\s*day|seventh\s*generation|method\b|attitude\b|cascade|platinum\s*actionpacs|finish|quantum\b|powerball|jet-?dry|rinse\s*aid|dishwasher\s*cleaner|affresh|lemi\s*shine|"
        r"trash\s*bag|garbage\s*bag|kitchen\s*bag|drawstring\s*bag|yard\s*waste\s*bag|glad|forceflex|hefty|kirkland\s*garbage|compost\s*bag|biobag|foil|aluminum\s*foil|alcan|reynolds\s*wrap|ziploc|freezer\s*bag|slider\s*bag|sandwich\s*bag|snack\s*bag|stasher|parchment|parchment\s*paper|if\s*you\s*care|cling\s*wrap|plastic\s*wrap|food\s*wrap|stretch-?tite|saran|press'n\s*seal|wax\s*paper|butcher\s*paper|freezer\s*paper|muffin\s*liner|cupcake\s*liner|disposable\s*plate|paper\s*plate|chinet|solo\s*cup|red\s*cup|plastic\s*cutlery|plastic\s*fork|toothpick|skewer|straws|"
        r"sponge|scotch-?brite|scrub\s*daddy|scrub\s*mommy|SOS\s*pad|steel\s*wool|mr\.\s*clean\s*magic\s*eraser|magic\s*eraser|microfiber\s*cloth|microfiber\s*towel|shop\s*towel|scott\s*shop|chamois|swiffer|wetjet|sweeper|duster\s*refill|vileda|easywring|promist|o-?cedar|spin\s*mop|mop\b|broom|push\s*broom|dustpan|squeegee|lint\s*roller|scotch-?gard| rubber\s*gloves|cleaning\s*gloves|"
        r"cleaner|all-?purpose\s*cleaner|multi-?surface|disinfectant|disinfecting\s*wipes|lysol|clorox\s*wipes|mr\.\s*clean|pine-?sol|fabuloso|spic\s*and\s*span|simple\s*green|krud\s*kutter|goo\s*gone|goof\s*off|windex|glass\s*cleaner|invisible\s*glass|sprayway|pledge|old\s*english|weiman|stainless\s*steel\s*cleaner|granite\s*gold|cerama\s*bryte|barkeepers\s*friend|vim\b|comet\b|ajax|bon\s*ami|soft\s*scrub|scrubbing\s*bubbles|kaboom|tilex|clr\b|lime-?a-?way|zep\b|pink\s*stuff|folex|bissell\s*formula|resolve\s*carpet|drain\s*cleaner|drano|liquid-?plumr|green\s*gobbler|ridx|septic|toilet\s*bowl|lysol\s*toilet|clorox\s*wand|scrubbing\s*bubbles\s*drop|poo-?pourri|febreze|air\s*wick|glade|ozium|damp-?rid|"
        r"storage\s*container|food\s*storage|meal\s*prep\s*container|deli\s*container|snapware|pyrex\s*storage|rubbermaid|brilliance|easy\s*find\s*lids|sistema|lock\s*&\s*lock|cambro|oxo\s*pop|tupperware|mason\s*jar|ball\s*jar|bernardin|bento\s*box|bentgo|lunch\s*box|lunch\s*bag|packit|ice\s*pack|"
        r"light\s*bulb|led\s*bulb|flood\s*light|string\s*lights|feit\s*electric|luminus|sylvania|philips\s*led|filtrete|3m\s*filter|furnace\s*filter|air\s*filter|merv\s*\d+|brita\s*filter|maxtra|pur\s*filter|command\s*strip|command\s*hook|gorilla\s*glue|gorilla\s*tape|krazy\s*glue|super\s*glue|jb\s*weld|loctite|duct\s*tape|scotch\s*tape|magic\s*tape|painter'?s\s*tape|frog\s*tape|packing\s*tape|shipping\s*tape|bubble\s*wrap|moving\s*box|bungee\s*cord|ratchet\s*strap|zip\s*tie|cable\s*tie|velcro|wd-?40|3-?in-?one|silicone\s*spray|"
        r"cat\s*litter|clumping\s*litter|arm\s*&\s*hammer\s*litter|tidy\s*cats|fresh\s*step|world'?s\s*best\s*cat|pretty\s*litter|litter\s*genie|litter\s*locker|dog\s*food|cat\s*food|pet\s*food|kibble|wet\s*cat\s*food|wet\s*dog\s*food|kirkland\s*nature'?s\s*domain|purina|pro\s*plan|friskies|fancy\s*feast|beneful|pedigree|iams|eukanuba|blue\s*buffalo|wellness\s*core|acana|orijen|royal\s*canin|hill'?s\s*science|nutrience|go!\s*solutions|now\s*fresh|open\s*farm|stella\s*&\s*chewy|whimzees|dentastix|greenies|bully\s*stick|milk-?bone|benebone|nylabone|kong\b|chuckit|churu|temptations|dog\s*treat|cat\s*treat|freeze\s*dried\s*liver|salmon\s*oil\s*for\s*dogs|cosequin\s*for\s*dogs|apoquel|frontline|advantage\s*ii|k9\s*advantix|pee\s*pad|training\s*pad|poop\s*bag|earth\s*rated|dog\s*bed|cat\s*tree|scratching\s*post|pet\s*carrier|bird\s*seed|suet|kaytee|"
        r"motor\s*oil|synthetic\s*oil|5w-?20|5w-?30|0w-?20|0w-?40|10w-?30|15w-?40|mobil\s*1|castrol|edge\s*oil|gtx\b|pennzoil|valvoline|shell\s*rotella|kirkland\s*oil|transmission\s*fluid|brake\s*fluid|coolant|antifreeze|prestone|fuel\s*injector|sea\s*foam|lucas\s*oil|techron|def\b|diesel\s*exhaust|windshield\s*wash|de-?icer|rain-?x|wiper\s*blade|michelin\s*wiper|bosch\s*icon|tirco|car\s*wash|meguiar|turtle\s*wax|chemical\s*guys|armor\s*all| Griot|lexol|invisible\s*glass|tire\s*shine|wheel\s*cleaner|floor\s*liner|weathertech|husky\s*liners|phantom\s*mat|seat\s*cover|car\s*cover|steering\s*wheel\s*cover|windshield\s*sunshade|jump\s*starter|noco\s*boost|battery\s*tender|ctek|tire\s*inflator|air\s*compressor|fanttik|viair|astroai|floor\s*jack|jack\s*stand|lug\s*wrench|torque\s*wrench|obd2|roof\s*rack|cargo\s*box|thule|yakima|hitch\s*rack|"
        r"ice\s*melt|road\s*salt|alaskan\s*ice\s*melter|safe\s*step|safe\s*paw|pest\s*control|raid\b|off!\b|deep\s*woods|thermacell|ortho\s*home\s*defense|roundup|killex|weed\s*b\s*gon|scotts\s*turf\s*builder|miracle-?gro|ant\s*trap|terro|combat\s*roach|mouse\s*trap|tomcat|victor\s*trap|Sticky\s*trap|propane\s*tank|worthington|flame\s*king|charcoal|kingsford|royal\s*oak|lump\s*charcoal|wood\s*pellets|smoking\s*chips|firestarter|firelog|duraflame|pine\s*mountain|lighter\s*fluid|bic\s*lighter|scripto)(?:s|es)?\b",
        re.I,
    ),
    "clothing": re.compile(
        r"\b(mens?|womens?|ladies|kids?|boys?|girls?|youth|toddler|infant|baby\s*wear|onesie|romper|sleeper|bodysuit|unisex|apparel|"
        r"pants?|jeans|denim|chinos?|khakis?|trousers?|slacks|cargo\s*pants|work\s*pants|lined\s*pants|fleece\s*lined|leggings?|tights|joggers?|sweatpants?|track\s*pants|lounge\s*pants|capris?|shorts?|boardshorts|cargo\s*shorts|bike\s*shorts|skirt|skort|dress|sundress|maxi\s*dress|"
        r"hoodie|sweatshirt|crewneck|pullover|quarter-?zip|1/4\s*zip|half-?zip|full-?zip|fleece|sherpa|sweater|cardigan|cashmere|merino\s*wool|vest|down\s*vest|puffer\s*vest|jacket|coat|parka|down\s*jacket|puffer|shacket|bomber\s*jacket|denim\s*jacket|leather\s*jacket|softshell|hardshell|windbreaker|rain\s*jacket|raincoat|trench\s*coat|peacoat|overcoat|snow\s*suit|ski\s*jacket|snow\s*pants|bib\s*overall|blazer|suit\s*jacket|flannel|plaid\s*shirt|"
        r"shirt|t-?shirt|tee\b|graphic\s*tee|long\s*sleeve|short\s*sleeve|polo|golf\s*shirt|dress\s*shirt|button-?down|oxford\s*shirt|chambray|linen\s*shirt|henley|blouse|tunic|peasant\s*top|tank\s*top|muscle\s*shirt|camisole|cami|crop\s*top|base\s*layer|thermal\s*top|thermal\s*bottom|long\s*johns|rashguard|"
        r"underwear|boxers?|boxer\s*briefs?|briefs?|trunks|bikini\s*brief|hipster|thong|boyshort|seamless\s*underwear|bra\b|sports\s*bra|wireless\s*bra|bralette|nursing\s*bra|undershirt|a-?shirt|socks?|ankle\s*socks|no-?show\s*socks|crew\s*socks|quarter\s*socks|knee\s*high|compression\s*socks|merino\s*socks|wool\s*socks|work\s*socks|thermal\s*socks|diabetic\s*socks|slipper\s*socks|"
        r"pyjamas?|pajamas?|pjs?\b|sleep\s*set|lounge\s*set|loungewear|nightgown|sleepshirt|chemise|robe\b|bathrobe|plush\s*robe|housecoat|swimsuit|swimwear|one-?piece\s*swim|tankini|bikini|swim\s*trunks|board\s*shorts|"
        r"shoes?|footwear|sneakers?|runners?|running\s*shoes|walking\s*shoes|trail\s*runners|cross\s*trainers|court\s*shoes|tennis\s*shoes|casual\s*shoes|dress\s*shoes|oxfords|derbies|loafers?|boat\s*shoes|moccasins?|espadrilles|flats|ballet\s*flats|heels|pumps|wedges|boots?|ankle\s*boots|chelsea\s*boots|combat\s*boots|work\s*boots|steel\s*toe|hiking\s*boots|rain\s*boots|winter\s*boots|snow\s*boots|duck\s*boots|slippers?|moccasin\s*slippers|scuff\s*slippers|sandals?|flip-?flops?|slides?|clogs?|water\s*shoes|"
        r"crocs|birkenstock|ugg\b|koolaburra|sorel|kamik|baffin|pajar|cougar\s*boots|hunter\s*boots|boggs?|blundstone|dr\.\s*martens|timberland|merrell|keens?|salomon|hoka|on\s*cloud|brooks|saucony|asics|mizuno|new\s*balance|skechers|go\s*walk|arch\s*fit|slip-?ins|vessis?|allbirds|heydude|vans|converse|clarks|rockport|cole\s*haan|ecco|geox|florsheim|sperry|teva|chaco|oofos|recovery\s*slide|dearfoams|isotoner|acorn\s*slippers|"
        r"gloves?|winter\s*gloves|ski\s*gloves|leather\s*gloves|running\s*gloves|work\s*gloves|wells\s*lamont|head\s*gloves|mittens?|hand\s*warmer|hat\b|toque|beanie|winter\s*hat|sun\s*hat|bucket\s*hat|baseball\s*cap|trucker\s*hat|balaclava|neck\s*warmer|neck\s*gaiter|buff\b|scarf|pashmina|shawl|wrap|earmuffs|belt|leather\s*belt|web\s*belt|ratchet\s*belt|suspenders|tie\b|necktie|wallet|cardholder|secrid|bellroy|handbag|purse|crossbody\s*bag|tote\s*bag|fanny\s*pack|belt\s*bag|lululemon|coach\b|michael\s*kors|kate\s*spade|longchamp|guess\b|fossil|herschel|fjallraven|kipling|vera\s*bradley|baggallini|"
        r"sunglasses|polarized\s*sunglasses|reading\s*glasses|readers|blue\s*light\s*glasses|ray-?ban|oakley|maui\s*jim|costa\s*del\s*mar|persol|carrera|gucci\s*glasses|prada\s*glasses|tom\s*ford|versace|burberry|invicta|seiko|citizen|casio|g-?shock|timex|tissot|bulova|movado|skagen|jewelry|necklace|pendant|chain|earrings|studs|hoops|bracelet|bangle|tennis\s*bracelet|ring\b|diamond|gold\s*karat|14k\b|18k\b|sterling\s*silver|pearl|swarovski|"
        r"puma|adidas|nike|jordan|under\s*armour|reebok|champion|fila|umbro|kappa|ellesse|diadora|oakley\s*apparel|hurley|quiksilver|billabong|rvca|volcom|o'neill|rip\s*curl|speedo|tyr\b|arena\b|columbia|the\s*north\s*face|north\s*face|patagonia|arc'teryx|helly\s*hansen|marmot|mountain\s*hardwear|outdoor\s*research|cotopaxi|fjallraven|kuhl|prana|vuori|lululemon|athleta|alo\s*yoga|fabletics|gymshark|eddie\s*bauer|ll\s*bean|lands'\s*end|pendleton|carhartt|dickies|cat\s*workwear|dakota|windriver|woods\b|chlorophylle|kanuk|quartz\s*co|moose\s*knuckles|canada\s*goose|mackage|soia\s*&\s*kyo|rudsak|noize|save\s*the\s*duck|gerry|spyder|obermeyer|rossignol|salomon\s*jacket|orvis|bass\s*pro|cabela|"
        r"calvin\s*klein|ck\b|tommy\s*hilfiger|tommy\s*bahama|ralph\s*lauren|polo\s*ralph|lacoste|nautica|izod|vineyard\s*vines|original\s*penguin|perry\s*ellis|kenneth\s*cole|cole\s*haan|marc\s*new\s*york|andrew\s*marc|michael\s*kors|dkny|karl\s*lagerfeld|eliza\s*j|tahari|jones\s*new\s*york|anne\s*klein|nine\s*west|vince\s*camuto|lucky\s*brand|true\s*religion|7\s*for\s*all\s*mankind|paige|ag\s*jeans|citizens\s*of\s*humanity|joe'?s\s*jeans|hudson\s*jeans|nydj|jag\s*jeans|silver\s*jeans|mavi|dish\s*jeans|parasuco|levi'?s|wrangler|lee\b|dockers|haggar|farah|ben\s*sherman|ted\s*baker|french\s*connection|allsaints|banana\s*republic|gap\b|old\s*navy|j\.?\s*crew|madewell|club\s*monaco|aritzia|frank\s*and\s*oak|rw&co|reitmans|penningtons|laura\b|melanie\s*lyne|suzy\s*shier|ricki'?s|cleo\b|bootlegger|warehouse\s*one|bluenotes|urban\s*planet|ardene|simons|roots\b|bench\b|buffalo\s*david\s*bitton|point\s*zero|lolë|lole|mondetta|mpg\s*sport|kyodan|tuff\s*athletics|head\s*sport|bc\s*clothing|english\s*laundry|weatherproof\s*vintage|32\s*degrees|heat\s*tech|uniqlo|muji|jockey|hanes|fruit\s*of\s*the\s*loom|gildan|champion|russell\s*athletic|stanfield|saxx|2undr|bn3th|meundies|tomboyx|knix|wacoal|triumph\b|playtex|bali\b|maidenform|warner'?s|wonderbra|spanx|shapermint|hue\b|kushyfoot|k\.?\s*bell|gold\s*toe|wigwam|smartwool|darn\s*tough|icebreaker|stance|bombas|featuress?|balega|kirkland\s*signature\s*(mens?|womens?|ladies|kids?|youth|logo|crewneck|hoodie|jeans|pants|shorts|shirt|tee|jacket|coat|vest|socks|boxer|briefs|shearling|slippers?))(?:s|es)?\b",
        re.I,
    ),
    "health": re.compile(
        r"\b(vitamin|multivitamin|prenatal|postnatal|supplement|softgels?|tablets?|capsules?|caplets?|lozenges?|chewables?|gummy\s*vitamin|effervescent|"
        r"magnesium|citrate|glycinate|bisglycinate|oxide|omega-?3|omega\s*3-?6-?9|fish\s*oil|krill\s*oil|algal\s*oil|cod\s*liver|flaxseed\s*oil|evening\s*primrose|mct\s*oil|coq10|ubiquinol|probiotic|prebiotic|postbiotic|culturelle|align\b|florastor|bio-?k|collagen|marine\s*collagen|bovine\s*collagen|vital\s*proteins|organika|within\s*us|youtheory|biotin|keratin|silica|melatonin|5-?htp|l-?theanine|gaba\b|valerian|passionflower|glucosamine|chondroitin|msm\b|osteo\s*bi-?flex|lakota|nem\b|turmeric|curcumin|boswellia|quercetin|resveratrol|nac\b|n-?acetyl|glutathione|alpha\s*lipoic|nad\+?\b|nmn\b|berberine|ashwagandha|rhodiola|maca|lion'?s\s*mane|reishi|cordyceps|chaga|turkey\s*tail|mushroom\s*complex|elderberry|sambucus|echinacea|oil\s*of\s*oregano|propolis|manuka\s*honey\s*lozenge|garlic\s*extract|ginseng|ginkgo\s*biloba|st\.?\s*john'?s\s*wort|saw\s*palmetto|milk\s*thistle|dandelion|artichoke\s*extract|cranberry\s*extract|d-?mannose|black\s*cohosh|evening\s*primrose|fenugreek|lutein|zeaxanthin|astaxanthin|bilberry|ocuvite|preservision|vitalux|zinc|iron\s*supplement|feramax|slow\s*fe|calcium|caltrate|citracal|potassium|selenium|chromium|iodine|kelp|boron|copper|manganese|folic\s*acid|folate|b-?complex|vitamin\s*b\d*|b12\b|methylcobalamin|vitamin\s*c\b|ascorbic|ester-?c|emergen-?c|airborne|sisu\b|vitamin\s*d\d*|d3\b|k2\b|vitamin\s*e\b|vitamin\s*k\b|centrum|one\s*a\s*day|jamieson|webber\s*naturals|natural\s*factors|canprev|aor\b|thorne|pure\s*encapsulations|nfh\b|genestra|metagenics|new\s*roots|prairie\s*naturals|platinum\s*naturals|now\s*foods|nature'?s\s*bounty|nature'?s\s*way|nature\s*made|garden\s*of\s*life|new\s*chapter|mega\s*food|rainbow\s*light|smartypants|olly\b|vitafusion|l'il\s*critters|flintstones|pediasure|"
        r"protein\s*powder|whey\s*protein|whey\s*isolate|casein|mass\s*gainer|plant\s*protein|pea\s*protein|vegan\s*protein|clear\s*whey|optimum\s*nutrition|gold\s*standard|leanfit|kaizen|diesel\s*protein|allmax|isoflex|biosteel|vega\s*one|vega\s*sport|orgain|sunwarrior|blessed\s*protein|ghost\s*whey|ryse\b|alani\s*nu\s*protein|premier\s*protein|fairlife\s*core\s*power|fairlife\s*nutrition|muscle\s*milk|six\s*star|pure\s*protein\s*shake|ensure|boost\b|glucerna|carnations?\s*breakfast|soylent|huel\b|creatine|monohydrate|bcaa|eaa\b|glutamine|citrulline|beta-?alanine|pre-?workout|c4\s*pre|electrolyte|hydration\s*multiplier|liquid\s*i\.?v|nuun\b|lmnt\b|dripdrop|pedialyte|organika\s*electrolytes|trace\s*minerals|greens\s*powder|athletic\s*greens|ag1\b|amazing\s*grass|bloom\s*greens|subi\b|red\s*beet\s*powder|"
        r"pain\s*relief|analgesic|advil|iba\b|ibuprofen|motrin|tylenol|acetaminophen|paracetamol|aleve|naproxen|aspirin|excedrin|midol|pamprin|voltaren|diclofenac|robax|robaxin|robaxacet|robaxisal|methocarbamol|muscle\s*relaxant|icy\s*hot|biofreeze|tiger\s*balm|salonpas|deep\s*relief|a535|rub\s*a535|capsaicin|lidocaine|aspercreme|kalaya|arnica|traumeel|"
        r"allergy|antihistamine|reactine|cetirizine|claritin|loratadine|desloratadine|aerius|zyrtec|allegra|fexofenadine|benadryl|diphenhydramine|blexten|rupall|flonase|fluticasone|nasonex|mometasone|nasacort|avamys|omnaris|otrivin|xlear|saline\s*spray|hydrasense|neilmed|sinus\s*rinse|neti\s*pot|breathe\s*right|nasal\s*strip|cold\s*&\s*flu|dayquil|nyquil|vicks|vaporub|vapoinhaler|sinex|theraflu|neo-?citran|tylenol\s*cold|advil\s*cold|sinutab|sudafed|pseudoephedrine|buckley'?s|mucinex|guaifenesin|robitussin|benylin|delsym|dextromethorphan|bronchokod|helixia|prospan|stodal|coryzalia|oscillococcinum|cold-?fx|echinamide|ricola|halls|strepsils|cepacol|fisherman'?s\s*friend|vicks\s*drops|chloraseptic|betadine\s*gargle|"
        r"metamucil|psyllium\s*husk|benefiber|inulin|fibre\s*choice|restoralax|miralax|polyethylene\s*glycol|lax-?a-?day|senokot|sennosides|dulcolax|bisacodyl|colace|docusate|fleet\s*enema|milk\s*of\s*magnesia|imodium|loperamide|pepto-?bismol|bismuth|kaopectate|tums\b|rolaids|gaviscon|alginic|zantac|famotidine|pepcid|nexium|esomeprazole|omeprazole|losec|prevacid|lansoprazole|pantoprazole|align\s*digestive|beano|gas-?x|simethicone|lactaid|lactase\s*enzyme|digestive\s*enzyme|papaya\s*enzyme|ibgard|fdgard|iberogast|gravol|dimenhydrinate|dramamine|bonine|meclizine|sea-?band|preparation\s*h|anusol|tucks\b|witch\s*hazel|canesten|monistat|clotrimazole|fluconazole|diflucan|azo\b|cystoplus|"
        r"shampoo|dry\s*shampoo|conditioner|deep\s*conditioner|hair\s*mask|leave-?in|hair\s*oil|hair\s*serum|hair\s*spray|mousse|styling\s*gel|pomade|hair\s*wax|hair\s*clay|root\s*touch-?up|hair\s*dye|hair\s*color|olaplex|k18\b|kerastase|redken|biolage|pureology|amika|ouai\b|living\s*proof|moroccanoil|verb\b|briogeo|shea\s*moisture|mielle|cantu|maui\s*moisture|monday\s*hair|native\s*hair|kristin\s*ess|marc\s*anthony|john\s*frieda|paul\s*mitchell|nexus|nexxus|bed\s*head|tigi\b|aussie|herbal\s*essences|pantene|head\s*&\s*shoulders|selsun\s*blue|nizoral|ketoconazole|t/gel|t/sal|dove\s*hair|tresemme|garnier\s*fructis|whole\s*blends|l'oreal\s*elvive|everpure|ogx\b|aveeno\s*hair|kirkland\s*moisture\s*shampoo|batiste|klora?ne|nioxin|minoxidil|rogaine|viviscal|nutrafol|"
        r"body\s*wash|shower\s*gel|bath\s*soak|bubble\s*bath|bath\s*bomb|bar\s*soap|liquid\s*hand\s*soap|foaming\s*hand\s*soap|antibacterial\s*soap|dial\b|softsoap|ivory\b|olay|old\s*spice|axe\b|degree\s*body|dove\s*men|dove\s*body|caress|lever\s*2000|zest\b|irish\s*spring|coast\s*soap|dr\.\s*bronner|dr\.\s*teals|method\s*body|native\s*body|caprina|goat'?s\s*milk|l'occitane|philosophy|sol\s*de\s*janeiro|tree\s*hut|body\s*scrub|exfoliant|loofah|"
        r"lotion|body\s*lotion|hand\s*lotion|foot\s*cream|heel\s*balm|flexitol|o'keeffe'?s|working\s*hands|healthy\s*feet|bag\s*balm|uddersly\s*smooth|moisturizer|face\s*cream|day\s*cream|night\s*cream|eye\s*cream|neck\s*cream|strivectin|roc\b|olay\s*regenerist|l'oreal\s*revitalift|neutrogena\s*hydro\s*boost|aveeno\s*calm|aveeno\s*daily|aveeno\s*eczema|cerave|cetaphil|eucerin|aquaphor|vaseline|lubriderm|gold\s*bond|jergens|nivea|palmer'?s\s*cocoa|hempz|st\.\s*ives|curél|curel|glaxal\s*base|complex\s*15|vanicream|la\s*roche-?posay|cicaplast|lipikar|tolériane|vichy\b|mineral\s*89|avène|avene|bioderma|sensibio|uriage|embryolisse|first\s*aid\s*beauty|ultra\s*repair|kiehl'?s|clinique|estee\s*lauder|lancome|shiseido|sk-?ii|sulwhasoo|laneige|innisfree|cosrx|snail\s*mucin|beauty\s*of\s*joseon|torriden|anua\b|round\s*lab|mediheal|dr\.\s*jart|belif|tatcha|glow\s*recipe|summer\s*fridays|drunk\s*elephant|paula'?s\s*choice|the\s*ordinary|inkey\s*list|good\s*molecules|byoma|bubble\s*skincare|versed|pixi\s*skintreats|thayers|dickinson|biore|hada\s*labo|meishoku|theralogic|daiso|"
        r"cleanser|face\s*wash|cleaning\s*balm|cleansing\s*oil|micellar\s*water|makeup\s*remover| Neutrogena\s*wipes|face\s*wipes|toner|essence|ampoule|serum|vitamin\s*c\s*serum|retinol|retinal|tretinoin|adapalene|differin|bakuchiol|hyaluronic\s*acid|niacinamide|salicylic\s*acid|bha\b|aha\b|glycolic\s*acid|lactic\s*acid|azelaic\s*acid|tranexamic|peptides?|ceramides?|centella|cica\b|tea\s*tree\s*oil|benzoyl\s*peroxide|panoxyl|clean\s*&\s*clear|clearasil|noxzema|stridex|oxy\s*pads|acne\s*patch|pimple\s*patch|mighty\s*patch|hero\s*cosmetics|peace\s*out|hydrocolloid|sheet\s*mask|clay\s*mask|eye\s*patch|grace\s*&\s*stella|peter\s*thomas\s*roth|"
        r"sunscreen|sunblock|spf\s*\d+|mineral\s*sunscreen|zinc\s*oxide\s*spf|tinted\s*spf|after\s*sun|aloe\s*gel|self\s*tanner|jergens\s*natural\s*glow|st\.\s*tropez|bondi\s*sands|coppertone|banana\s*boat|hawaiian\s*tropic|australian\s*gold|sun\s*bum|blue\s*lizard|thinkbaby|thinksport|alba\s*botanica|ombrelle|anthelios|eltamd|supergoop|unseen\s*sunscreen|coola\b|Vacation\s*spf|lip\s*balm|chapstick|blistex|carmex|eos\b|burt'?s\s*bees|vaseline\s*lip|aquaphor\s*lip|laneige\s*lip|nivea\s*lip|maybelline\s*baby\s*lips|sun\s*bum\s*lip|"
        r"makeup|cosmetics?|foundation|concealer|tinted\s*moisturizer|bb\s*cream|cc\s*cream|it\s*cosmetics|primer|setting\s*powder|setting\s*spray|blush|bronzer|highlighter|contour|eyeshadow|eyeliner|mascara|lash\s*princess|sky\s*high|telescopic|falsies|false\s*lashes|ardell|kiss\s*lashes|lash\s*serum|grande\s*lash|babe\s*lash|rapid\s*lash|brow\s*pencil|brow\s*gel|nyx\b|e\.l\.f\.|milani|wet\s*n\s*wild|essence\s*makeup|catrice|maybelline|l'oreal\s*paris|revlon|covergirl|rimmel|physicians\s*formula|sally\s*hansen|essie|opi\b|nail\s*polish|nail\s*clipper|tweezers|twerman|tweezerman|eyelash\s*curler|makeup\s*brush|real\s*techniques|eco\s*tools|beautyblender|makeup\s*sponge|cotton\s*rounds|cotton\s*pads|swisspers|q-?tips|cotton\s*swabs|"
        r"toothpaste|whitening\s*paste|whitening\s*strips|crest\s*3d\s*white|colgate|total\b|optic\s*white|crest|pro-?health|scope\b|sensodyne|pronamel|parodontax|arm\s*&\s*hammer\s*paste|tom'?s\s*of\s*maine|hello\s*paste|boka\b|davids\s*paste|marvis|biotene|mouthwash|listerine|therabreath|act\s*rinse|cepacaol|floss|dental\s*floss|glide\s*floss|plackers|floss\s*picks|gum\s*soft-?picks|interdental\s*brush|proxabrush|tongue\s*scraper|toothbrush\s*heads|replacement\s*heads|denture\s*cleaner|polident|efffferdent|fixodent|seabond|night\s*guard|"
        r"deodorant|antiperspirant|whole\s*body\s*deodorant|lume\b|mando\b|degree|ultraclear|secret\b|clinical\s*strength|gillette\s*clear\s*gel|old\s*spice\s*deo|dove\s*advanced\s*care|dove\s*0%|mitchum|ban\s*roll-?on|speed\s*stick|lady\s*speed|suave\s*deo|arrid|right\s*guard|arm\s*&\s*hammer\s*deo|native\s*deo|schmidt'?s|tom'?s\s*deo|kopari|crystal\s*deodorant|alvera|"
        r"razor|razor\s*blades|refill\s*cartridges|disposable\s*razors|safety\s*razor|dermaplaning|tinkle|flamingo\s*razor|billie\s*razor|harry'?s|dollar\s*shave|gillette|mach3|fusion5|proglide|labs\s*razor|skinguard|venus|comfortglide|schick|hydro\s*5|hydro\s*silk|intuition|quattro|treme\s*3|bic\s*soleil|bic\s*flex|wilkinson\s*sword|feather\s*blades|shave\s*gel|shave\s*foam|shaving\s*cream|aftershave|nivea\s*men\s*shave|edge\s*shave|skintimate|eos\s*shave|barbasol|proraso|cremo|aveeno\s*shave|tend\s*skin|bump\s*patrol|wax\s*strips|veet|nair|sally\s*hansen\s*wax|"
        r"eye\s*drops?|artificial\s*tears|lubricant\s*eye|systane|ultra\b|complete\b|hydration\s*pf|refresh\s*tears|refresh\s*optive|refresh\s*celluvisc|blink\s*tears|theratears|hylo\b|thealoz|i-?drop|visine|clear\s*eyes|lumify|zaditor|pataday|ocuhist|stye\b|blephamide|occusoft|eyelid\s*wipes|contact\s*solution|multi-?purpose\s*solution|hydrogen\s*peroxide\s*solution|clear\s*care|opti-?free|puremoist|replenish|biotrue|re-?nu|boston\s*simplus|kirkland\s*contact|"
        r"ear\s*drops|debrox|murine\s*ear|mack'?s\s*earplugs|loop\s*earplugs|bandage|band-?aid|tough\s*strips|hydro\s*seal|elastoplast|nexcare|curad|tegaderm|liquid\s*bandage|new-?skin|gauze|non-?stick\s*pad|medical\s*tape|paper\s*tape|coban|self-?adherent|ace\s*bandage|tensor\s*bandage|knee\s*brace|ankle\s*brace|wrist\s*brace|back\s*brace|futuro|mueller|bauerfeind|kt\s*tape|kinesiology|polysporin|neosporin|bacitracin|fucidin|betadine|antiseptic|wound\s*wash|first\s*aid\s*kit|rubbing\s*alcohol|isopropyl|アルコール|hydrogen\s*peroxide|witch\s*hazel|calamine|hydrocortisone|cortate|cortizone-?10|benadryl\s*itch|gold\s*bond\s*medicated|after\s*bite|nix\b|kwellada|lice\s*treatment|compound\s*w|wart\s*remover|dr\.\s*scholl|corn\s*cushion|bunion|plantar\s*fasciitis|insoles|orthotics|superfeet|powerstep|lamisil|terbinafine|tinactin|lotrimin|daktarin|athlete'?s\s*foot|jock\s*itch|nail\s*fungus|emuaid|kerasal|"
        r"epsom\s*salt|sitz\s*bath|heating\s*pad|sunbeam\s*heat|hot\s*water\s*bottle|magic\s*bag|ice\s*pack|cryomax|tens\s*unit|omron|blood\s*pressure|bp\s*monitor|bios\s*medical|braun\s*thermoscan|thermometer|pulse\s*oximeter|stethoscope|glucometer|freestyle\s*libre|contour\s*next|accu-?chek|lancets|test\s*strips|glucose\s*tablets|dex4|cpap\s*mask|cpap\s*cleaner|resmed|philips\s*respironics|distilled\s*water\s*for\s*cpap|humidifier\s*chamber|cane\b|walker\b|rollator|shower\s*chair|grab\s*bar|commode|incontinence|depends|poise|tena\b|always\s*discreet|prevail|attends|adult\s*briefs|underpads|feminine\s*hygiene|always\s*pads|infinity\s*flexfoam|ultra\s*thin|kotex|u\s*by\s*kotex|stayfree|carefree|pantyliner|tampax|pearl\b|radiant|playtex\s*sport|o\.b\.\s*tampons|diva\s*cup|menstrual\s*cup|saalt|thinx|period\s*underwear|vagisil|summer'?s\s*eve|monistat\s*chafing|lubricant|k-?y\s*jelly|astroglide|durex|trojan|condoms|pregnancy\s*test|clearblue|first\s*response|ovulation\s*test|diapers?|huggies|little\s*snugglers|little\s*movers|pull-?ups|pampers|swaddlers|cruisers|baby\s*dry|easy\s*ups|kirkland\s*diapers|rascals|honest\s*diapers|hello\s*bello|millie\s*moon|goodnites|ninjamas|swim\s*diapers|baby\s*wipes|rico\s*wipes|waterwipes|pampers\s*aqua|huggies\s*natural|desitin|penaten|sudocrem|boudreaux'?s\s*butt|diaper\s*rash|baby\s*powder|baby\s*oil|baby\s*lotion|baby\s*wash|aveeno\s*baby|cetaphil\s*baby|mustela|live\s*clean\s*baby|gripe\s*water|mommy'?s\s*bliss|oogiebear|frida\s*baby|nosefrida|infant\s*formula|enfamil|a\+\b|similac|pro-?advance|alimentum|nutramigen|good\s*start|kendamil|kirkland\s*formula|baby\s*cereal|gerber|love\s*child|baby\s*gourmet|pc\s*organics\s*baby)(?:s|es)?\b",
        re.I,
    ),
    "home": re.compile(
        r"\b(pillow|bed\s*pillow|down\s*pillow|feather\s*pillow|memory\s*foam\s*pillow|cooling\s*pillow|body\s*pillow|throw\s*pillow|accent\s*pillow|squishmallow|sheet|bed\s*sheet|sheet\s*set|fitted\s*sheet|flat\s*sheet|flannel\s*sheet|microfiber\s*sheet|cotton\s*sheet|egyptian\s*cotton|pima\s*cotton|supima|percale|sateen|bamboo\s*sheet|linen\s*sheet|thread\s*count|\d{3,4}\s*tc\b|pillowcase|silk\s*pillowcase|blissy|mulberry\s*silk|blanket|weighted\s*blanket|fleece\s*blanket|muslin\s*blanket|throw\b|plush\s*throw|faux\s*fur\s*throw|knitted\s*throw|heated\s*throw|electric\s*blanket|heated\s*mattress\s*pad|duvet|duvet\s*cover|down\s*duvet|comforter|down\s*alternative|quilt|coverlet|bedspread|bed\s*in\s*a\s*bag|mattress|innerspring|hybrid\s*mattress|pocket\s*coil|sealy|posturepedic|serta|perfect\s*sleeper|simmons|beautyrest|stearns\s*&\s*foster|kingsdown|novaform|casper|purple\s*mattress|ghostbed|nectar\s*mattress|endy\b|douglas\s*mattress|silk\s*&\s*snow|tempur-?pedic|mattress\s*pad|mattress\s*topper|mattress\s*protector|encasement|allergen\s*barrier|memory\s*foam|gel\s*foam|"
        r"towel|bath\s*towel|bath\s*sheet|hand\s*towel|washcloth|face\s*cloth|beach\s*towel|turkish\s*towel|microfiber\s*hair\s*towel|charisma\s*towel|loftex|grand\s*patrician|bath\s*mat|memory\s*foam\s*mat|chenille\s*mat|diatomaceous\s*mat|stone\s*bath\s*mat|shower\s*curtain|shower\s*liner|shower\s*rings|shower\s*caddy|tension\s*pole|shower\s*head|rain\s*shower|handheld\s*shower|filtered\s*shower|waterpik\s*shower|moen|kohler|delta\s*faucet|grohe|hansgrohe|pfister|american\s*standard|kraus|water\s*ridge|faucet|kitchen\s*faucet|bathroom\s*faucet|vanity|medicine\s*cabinet|toilet\b|one-?piece\s*toilet|two-?piece\s*toilet|elongated\s*toilet|bidet|bidet\s*seat|washlet|brondell|swash|toto\b|bio\s*bidet|tushy|plunger|toilet\s*brush|"
        r"rug\b|area\s*rug|shag\s*rug|oriental\s*rug|persian\s*rug|jute\s*rug|outdoor\s*rug|washable\s*rug|ruggable|loloi|nourison|safavieh|thomasville\s*rug|runner|hallway\s*runner|stair\s*tread|doormat|welcome\s*mat|coir\s*mat|scraper\s*mat|anti-?fatigue\s*mat|kitchen\s*mat|monoprice\s*mat|curtain|drape|drapery|window\s*panel|sheer\s*curtain|blackout\s*curtain|thermal\s*curtain|curtain\s*rod|blind\b|roller\s*shade|cellular\s*shade|roman\s*shade|faux\s*wood\s*blind|shutters|"
        r"hanger|velvet\s*hanger|wood\s*hanger|pant\s*hanger|storage\s*bin|storage\s*box|storage\s*tote|clear\s*bin|underbed\s*storage|greenmade|insta-?crate|collapsible\s*crate|sterilite|iris\s*storage|rubbermaid\s*roughneck|actionpacker|deck\s*box|keter|suncast|lifetime\s*shed|basket|woven\s*basket|wire\s*basket|seagrass\s*basket|hamper|laundry\s*hamper|laundry\s*basket|laundry\s*sorter|drying\s*rack|clothesline|ironing\s*board|brabantia|organizer|drawer\s*organizer|closet\s*organizer|hanging\s*organizer| turntable\s*organizer|lazy\s*susan|spice\s*rack|over\s*the\s*door|command\s*center|shoe\s*rack|shoe\s*bench|shoe\s*cabinet|coat\s*rack|hall\s*tree|umbrella\s*stand|shelving|wire\s*shelving|storage\s*rack|utility\s*rack|heavy\s*duty\s*rack|garage\s*rack|overhead\s*storage|Trinity\s*rack|gorilla\s*rack|whalen|gladiator|safe\b|fire\s*safe|gun\s*safe|security\s*safe|lockbox|sentrysafe|first\s*alert\s*safe|browning\s*safe|sanctuary\s*safe|"
        r"lamp\b|floor\s*lamp|torchiere|arc\s*lamp|table\s*lamp|bedside\s*lamp|touch\s*lamp|desk\s*lamp|task\s*lamp|architect\s*lamp|ottlite|salt\s*lamp|lava\s*lamp|sconce|wall\s*light|pendant\s*light|chandelier|flush\s*mount|vanity\s*light|track\s*lighting|shop\s*light|work\s*light|under\s*cabinet\s*light|puck\s*light|motion\s*sensor\s*light|night\s*light|book\s*light|string\s*light|edison\s*bulb|cafe\s*lights|solar\s*lights|pathway\s*lights|landscape\s*lighting|rope\s*light|led\s*strip|fairy\s*lights|flashlight|tactical\s*flashlight|rechargeable\s*flashlight|duracell\s*flashlight|coast\s*flashlight|olight|fenix\s*light|maglite|lantern|camping\s*lantern|headlamp|petzl|black\s*diamond\s*lamp|"
        r"candle|scented\s*candle|soy\s*candle|3-?wick|jar\s*candle|pillar\s*candle|tea\s*light|flameless\s*candle|led\s*candle|candle\s*warmer|yankee\s*candle|woodwick|chesapeake\s*bay|sand\s*\+\s*fog|dw\s*home|voluspa|nest\s*candle|diptyque|bath\s*&\s*body\s*works|village\s*candle|diffuser|reed\s*diffuser|ultrasonic\s*diffuser|aroma\s*diffuser|vitruvi|saje\b|essential\s*oil|lavender\s*oil|peppermint\s*oil|eucalyptus\s*oil|mirror|full\s*length\s*mirror|wall\s*mirror|vanity\s*mirror|lighted\s*mirror|simplehuman\s*mirror|clock|wall\s*clock|alarm\s*clock|atomic\s*clock|frame|picture\s*frame|photo\s*frame|collage\s*frame|poster\s*frame|shadow\s*box|wall\s*art|canvas\s*print|metal\s*art|sign\b|vase\b|artificial\s*plant|faux\s*plant|faux\s*tree|fiddle\s*leaf|olive\s*tree|monstera|succulent|wreath|garland|"
        r"desk\b|standing\s*desk|sit-?stand|electric\s*desk|writing\s*desk|computer\s*desk|l-?shaped\s*desk|corner\s*desk|lap\s*desk|tresanti|flexispot|uplift|branch\s*ergonomic|chair|office\s*chair|mesh\s*chair|task\s*chair|executive\s*chair|ergonomic\s*chair|herman\s*miller|aeron|embody|steelcase|leap\b|secretlab|gaming\s*chair|dps\s*gaming|la-?z-?boy|true\s*innovations|accent\s*chair|armchair|club\s*chair|swivel\s*chair|papasan|bean\s*bag|lovesac|dining\s*chair|parsons\s*chair|folding\s*chair|stakmore|cosco\s*chair|recliner|power\s*recliner|lift\s*chair|glider|rocking\s*chair|ottoman|storage\s*ottoman|pouf|footstool|bench\b|storage\s*bench|entryway\s*bench|bar\s*stool|counter\s*stool|swivel\s*stool|sofa|couch|sectional|modular\s*sofa|sleeper\s*sofa|sofa\s*bed|loveseat|chaise|futon|daybed|thomasville|aidan\s*&\s*ivy|gilman\s*creek|natuzzi|ashley\s*furniture|west\s*elm|crate\s*&\s*barrel|pottery\s*barn|article\b|structube|ikea|cozey|"
        r"table|dining\s*table|dining\s*set|coffee\s*table|end\s*table|side\s*table|accent\s*table|console\s*table|sofa\s*table|nesting\s*tables|c-?table|tv\s*stand|media\s*console|entertainment\s*center|electric\s*fireplace|dimplex|nightstand|bedside\s*table|dresser|chest\s*of\s*drawers|armoire|wardrobe|headboard|bed\s*frame|platform\s*bed|adjustable\s*base|bunk\s*bed|murphy\s*bed|bookcase|bookshelf|cube\s*organizer|kallax|billy\b|cabinet|accent\s*cabinet|storage\s*cabinet|pantry\s*cabinet|curio\s*cabinet|bar\s*cabinet|buffet|sideboard|credenza|hutch|kitchen\s*island|kitchen\s*cart|bar\s*cart|folding\s*table|banquet\s*table|card\s*table|lifetime\s*table|step\s*stool|cosco\s*step|ladder|step\s*ladder|extension\s*ladder|telescoping\s*ladder|little\s*giant|werner|gorilla\s*ladder|featherlite|"
        r"patio|patio\s*set|outdoor\s*furniture|conversation\s*set|bistro\s*set|outdoor\s*sectional|outdoor\s*dining|wicker|rattan|cast\s*aluminum|teak|polywood|agIo|sunvilla|sirio|ove\s*decors|gazebo|hardtop\s*gazebo|pop-?up\s*canopy|pergola|pavilion|sunroom|shed\b|storage\s*shed|yardistry|sojag|palram|canopia|sunjoy|shelterlogic|umbrella|patio\s*umbrella|cantilever\s*umbrella|market\s*umbrella|umbrella\s*base|sunbrella|adirondack|muskoka\s*chair|chaise\s*lounge|zero\s*gravity\s*chair|hammock|porch\s*swing|egg\s*chair|fire\s*pit|fire\s*table|propane\s*fire|solo\s*stove|bonfire|yukon\b|ranger\b|breeo|outland\s*living|planter|flower\s*pot|raised\s*garden\s*bed|cedar\s*planter|self-?watering|potting\s*soil|miracle-?gro\s*soil|pro-?mix|compost|peat\s*moss|mulch|bark\s*mulch|cedar\s*mulch|topsoil|fertilizer|plant\s*food|osmocote|grass\s*seed|scotts\s*ez\s*seed|flower\s*bulbs|perennials|annuals|hanging\s*basket|poinsettia|christmas\s*tree|holiday\s*wreath|garland|lawn\s*edging|landscape\s*fabric|weed\s*barrier|wheelbarrow|garden\s*cart|gorilla\s*cart|pruner|secateurs|fiskars|felco|loppers|hedge\s*shears|garden\s*trowel|spade|shovel|snow\s*shovel|roof\s*rake|snow\s*brush|ice\s*scraper|snow\s*joe|garant|true\s*temper|"
        r"hose|garden\s*hose|expandable\s*hose|zero-?g\s*hose|flexzilla|pocket\s*hose|hose\s*reel|hose\s*nozzle|sprinkler|oscillating\s*sprinkler|soaker\s*hose|orbit\b|melnor|gilmour|gardena|rain\s*barrel|pressure\s*washer|gas\s*pressure\s*washer|electric\s*pressure\s*washer|surface\s*cleaner|foam\s*cannon|simpson\s*washer|ryobi\s*washer|sun\s*joe|greenworks\s*washer|karcher\s*washer|simoniz|snow\s*blower|snow\s*thrower|ariens|toro\s*snow|cub\s*cadet|ego\s*power|generator|inverter\s*generator|dual\s*fuel|tri-?fuel|firman|champion\s*generator|honda\s*generator|westinghouse\s*generator|generac|predator\s*generator|lawn\s*mower|push\s*mower|self-?propelled|riding\s*mower|robot\s*mower|string\s*trimmer|weed\s*wacker|edger|hedge\s*trimmer|pole\s*saw|chainsaw|leaf\s*blower|wood\s*chipper|log\s*splitter|rototiller|cultivator|dethatcher|aerator|greenworks|ego\b|worx|sun\s*joe|husqvarna|stihl|echo\s*power|toro\b|honda\s*mower|"
        r"dewalt|milwaukee|m12\b|m18\b|makita|bosch\s*tools|ryobi|ridgid|craftsman|kobalt|husky\b|stanley\s*tools|black\+decker\s*tools|skil\b|metabo|hitachi|festool|dremel|wagner\s*spray|klein\s*tools|knipex|channellock|irwin|vise-?grip|crescent|gearwrench|wera\b|wiha\b|tekton|tool\s*set|mechanic'?s\s*tool|socket\s*set|ratchet|wrench|allen\s*wrench|hex\s*key|screwdriver|bit\s*set|pliers|wire\s*stripper|hammer|mallet|pry\s*bar|utility\s*knife|box\s*cutter|olfa|tape\s*measure|laser\s*level|stud\s*finder|zircon|franklin\s*sensors|clamp|vise|workbench|sawhorse|tool\s*box|tool\s*chest|tool\s*cabinet|packout|toughsystem|tstak|drill|impact\s*driver|hammer\s*drill|circular\s*saw|miter\s*saw|table\s*saw|jigsaw|reciprocating\s*saw|sawzall|oscillating\s*tool|multi-?tool|sander|orbital\s*sander|angle\s*grinder|router\s*tool|planer|nailer|brad\s*nailer|framing\s*nailer|staple\s*gun|glue\s*gun|heat\s*gun|soldering\s*iron|multimeter|shop\s*light|"
        r"luggage|suitcase|carry-?on|spinner\s*luggage|hardside|softside|checked\s*bag|luggage\s*set|packing\s*cubes|garment\s*bag|duffel\s*bag|weekender|travel\s*bag|backpack|laptop\s*backpack|hiking\s*backpack|daypack|sling\s*bag|lunch\s*cooler|samsonite|american\s*tourister|delsey|ricardo\s*beverly|travelpro|briggs\s*&\s*riley|tumi\b|away\s*luggage|monos\b|beis\b|swissgear|wenger|victorinox\s*luggage|thule\s*bag|osprey|deuter|gregory\b|camelbak|kelty|high\s*sierra|jansport|dakine|patagonia\s*bag|north\s*face\s*bag|ogio|ful\b|skyway|hey's|heys|champs\s*luggage|root\s*bags|"
        r"cooler|hard\s*cooler|soft\s*cooler|wheeled\s*cooler|rotomolded|beverage\s*jug|igloo|coleman\s*cooler|yeti\b|tundra\b|roadie|hopper\b|rambler|rtic|orca\s*cooler|pelican\s*cooler|rovic|titan\s*deep\s*freeze|arctic\s*zone|tommy\s*bahama\s*cooler|stanley\s*quencher|stanley\s*cup|Stanley\s*thermos|thermos\b|hydro\s*flask|contigo|owala|freesip|takeya|iron\s*flask|simple\s*modern|brumate|zulu\b|camelbak\s*bottle|nalgene|blenderbottle|shaker\s*bottle|corkcicle|swell\b|s'well|klein\s*kanteen|manna\b|reducetumbler|tumbler|insulated\s*mug|travel\s*mug|water\s*bottle|hydration\s*pack|"
        r"tent\b|camping\s*tent|instant\s*tent|screen\s*house|canopy\s*tent|coleman\s*tent|core\s*equipment|gazelle\s*tent|msr\b|big\s*agnes|rei\b|kelty\s*tent|sleeping\s*bag|sleeping\s*pad|air\s*mattress|aerobed|intex|bestway|camping\s*cot|camping\s*chair|folding\s*camp\s*chair|rocker\s*chair|gci\s*outdoor|helinox|kuma\s*outdoor|woods\s*camping|camp\s*stove|jetboil|camp\s*kitchen|trekking\s*poles|cascade\s*mountain|snowshoes|crampons|yaktrax|kayak|inflatable\s*kayak|pelican\s*kayak|lifetime\s*kayak|intex\s*kayak|sevylor|paddle\s*board|sup\b|inflatable\s*sup|body\s*glove\s*board|hyper\s*lite|tobin\s*sports|hoot\s*board|life\s*jacket|pfd\b|mustang\s*survival|stearns\s*pfd|wetsuit|snorkel\s*set|us\s*divers|goggles|speedo\s*goggles|pool\s*float|lounger|above\s*ground\s*pool|bestway\s*pool|intex\s*pool|hot\s*tub|inflatable\s*spa|saluspa|lay-?z-?spa|pool\s*chlorine|pool\s*shock|aquachek|"
        r"golf|golf\s*balls|golf\s*clubs|driver\b|irons\s*set|putter|wedge\b|golf\s*bag|golf\s*cart|push\s*cart|caddytek|golf\s*glove|callaway|warbird|hex\s*tour|edge\s*set|taylor\s*made|taylormade|titleist|pro\s*v1|bridgestone\s*golf|srixon|mizuno\s*golf|ping\b|cobra\s*golf|wilson\s*staff|pxg\b|kirkland\s*signature\s*golf|pickleball|pickleball\s*paddle|selkirk|joola|onenix|head\s*pickleball|franklin\s*sports|tennis\s*racket|badminton|yonex|basketball|spalding|wilson\s*ncaa|soccer\s*ball|volleyball|mikasa|football|baseball\s*glove|rawlings|easton|louisville\s*slugger|hockey\s*stick|bauer\b|ccm\b|sher-?wood|warrior\s*hockey|hockey\s*tape|skates|inline\s*skates|rollerblades|scooter|kick\s*scooter|electric\s*scooter|segway|ninebot|niU\b|apollo\s*scooter|bicycle|bike\b|mountain\s*bike|road\s*bike|cruiser\s*bike|fat\s*bike|e-?bike|electric\s*bike|jetson|rad\s*power|aventon|lectric|trek\b|giant\s*bike|specialized|cannondale|northrock|schwinn|huffy|mongoose|bike\s*helmet|giro\b|bell\s*helmet|mips\b|ski\s*helmet|ski\s*goggles|smith\s*optics|bolle|spy\s*optic|bike\s*lock|kryptonite|abus\b|bike\s*pump|bike\s*rack|"
        r"dumbbell|adjustable\s*dumbbell|bowflex|selecttech|powerblock|nuobell|kettlebell|barbell|weight\s*plate|bumper\s*plate|trap\s*bar|weight\s*bench|squat\s*rack|power\s*rack|pull-?up\s*bar|resistance\s*band|trx\b|medicine\s*ball|slam\s*ball|bosu\b|ab\s*wheel|push-?up\s*board|yoga\s*mat|manduka|gaiam|liforme|exercise\s*mat|gym\s*flooring|puzzle\s*mat|foam\s*roller|triggerpoint|chirp\s*wheel|massage\s*gun|theragun|therabody|hypervolt|hyperice|air\s*relax|normatec|treadmill|walking\s*pad|peloton|echelon|nordictrack|proform|sole\s*fitness|schwinn\s*ic4|spin\s*bike|exercise\s*bike|air\s*bike|rogue\s*echo|concept2|rower|rowing\s*machine|hydrow|waterrower|elliptical|stair\s*stepper|home\s*gym|"
        r"lego\b|duplo|playmobil|hot\s*wheels|matchbox|nerf\b|barbie|monster\s*high|l\.o\.l\.\s*surprise|american\s*girl|our\s*generation|calico\s*critters|sylvanian|paw\s*patrol|bluey|peppa\s*pig|cocomelon|gabby'?s\s*dollhouse|disney\s*princess|marvel|avengers|spider-?man|star\s*wars|transformers|hasbro|mattel|fisher-?price|little\s*tikes|step2|kidkraft|melissa\s*&\s*doug|vtech|leapfrog|crayola|play-?doh|kinetic\s*sand|slime|board\s*game|monopoly|catan|ticket\s*to\s*ride|carcassonne|azul\b|wingspan|codenames|exploding\s*kittens|uno\b|jenga|scrabble|clue\b|risk\b|chess|mahjong|poke?mon\s*cards|tcg\b|magic\s*the\s*gathering|lorcana|jigsaw\s*puzzle|ravensburger|buffalo\s*games|tonies|toniebox|yoto\b|book\b|hardcover|paperback|box\s*set|cookbook|novel|coloring\s*book|activity\s*book|sticker\s*book|pen\b|ballpoint|gel\s*pen|rollerball|sharpie|marker|highlighter|pencil|mechanical\s*pencil|eraser|notebook|moleskine|leuchtturm|planner|calendar|post-?it|sticky\s*notes|index\s*cards|binder|folder|sheet\s*protector|copy\s*paper|printer\s*paper|envelopes|scissors|scotch\s*scissors|westcott|stapler|swingline|paper\s*clips|binder\s*clips|elmer'?s\s*glue|glue\s*stick|mod\s*podge|acrylic\s*paint|canvas\s*pack|sketchbook|origami|yarn\b|knitting|crochet|sewing\s*machine|singer\b|brother\s*sewing)(?:s|es)?\b",
        re.I,
    ),
}

HEALTH_UNIT_RE = re.compile(
    r"\b\d+\s*(?:x\s*\d+\s*)?(?:softgels?|tablets?|capsules?|caplets?|lozenges?|gummies|chewables?|vials?|ampoules?|patches|suppositories|mcg|mg\b|iu\b|cfu\b|spf\s*\d+)\b",
    re.I,
)
# Notice NO trailing \b after \" so '65" CLASS' matches properly!
ELEC_UNIT_RE = re.compile(
    r"(?:\b\d{2,3}(?:\.\d)?\s*(?:\"|''|inch(?:es)?|-inch)\b|\b\d{2,3}(?:\.\d)?\s*\"|\b\d+\s*(?:tb|gb|mhz|ghz|lumens?|nits?|mah)\b|\b(?:1080p|1440p|2160p|4k|8k|uhd|fhd|qhd|oled|qled|mini-?led|wi-?fi\s*[567e]|bluetooth|usb-?c|hdmi\s*2\.\d|rtx\s*\d{4}|ryzen\s*[3579]|core\s*(?:i[3579]|ultra)|m[1234]\s*(?:chip|pro|max))\b)",
    re.I,
)
# Food weight/volume fallback when no other non-grocery category matched
FOOD_UNIT_RE = re.compile(
    r"\b(?:\d+\s*x\s*)?\d+(?:\.\d+)?\s*(?:kg|g\b|grams?|lb|lbs|oz)\b",
    re.I,
)
NON_FOOD_CUE_RE = re.compile(
    r"\b(spf|shampoo|conditioner|lotion|cream|serum|cleanser|soap|body\s*wash|toothpaste|mouthwash|deodorant|razor|wipes|diaper|detergent|cleaner|disinfect|bleach|paper|towel|tissue|litter|fertilizer|grass|seed|soil|salt|melt|charcoal|pellet|propane|oil\s*\d+w|motor|candle|wax|pillow|sheet|blanket|throw|duvet|mattress|rug|mat|towel|shirt|pant|jean|jacket|coat|sock|shoe|boot|glove|hat|bag|pack|luggage|tool|wrench|drill|saw|battery|batteries|bulb|led|cable|charger|speaker|camera|watch|toy|lego|game|book|pen|dumbbells?|weight)\b",
    re.I,
)


def detect_category(label: str) -> str:
    if not label:
        return "other"
    # Strip parenthetical savings notes so words inside "($5 INSTANT SAVINGS...)" don't skew scoring
    clean = PAREN_RE.sub(" ", label)

    for rx, cat in CATEGORY_OVERRIDES:
        if rx.search(clean):
            return cat

    scores = Counter()
    for cat, pattern in CATEGORY_RULES.items():
        hits = pattern.findall(clean)
        if hits:
            scores[cat] += len(hits)

    if HEALTH_UNIT_RE.search(clean):
        scores["health"] += 2
    if ELEC_UNIT_RE.search(clean):
        scores["electronics"] += 2

    if scores:
        return scores.most_common(1)[0][0]

    # Fallback: if an unclassified item has a food weight (e.g. "454 G", "1.2 KG") and no non-food cues, it's almost certainly a grocery item
    if FOOD_UNIT_RE.search(clean) and not NON_FOOD_CUE_RE.search(clean):
        return "groceries"

    return "other"

def strip_savings(label: str) -> str:
    text = label or ""
    for rx in (SAVE_RE, OFF_RE, REG_RE):
        text = rx.sub(" ", text)
    return text


def parse_price(text: str):
    cleaned = PAREN_RE.sub(" ", text or "")
    found = PRICE_RE.findall(cleaned)
    return found[-1] if found else None


def _money(s: str) -> float:
    return float(s.replace(",", ""))


def _sane_pct(pct):
    return pct if pct is not None and 1 <= pct <= 90 else None


def compute_discount_pct(label: str, sale: float | None):
    label = label or ""
    m = PCT_OFF_RE.search(label)
    if m:
        return _sane_pct(int(m.group(1)))
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


@dataclass
class Item:
    url: str
    label: str
    alt: str
    alt_key: str
    file_key: str
    item_no: str
    category: str
    price: float | None = field(init=False, default=None)
    disc_pct: int | None = field(init=False, default=None)
    clearance: bool = field(init=False, default=False)
    end_date: str = field(init=False, default="")
    ident: str = field(init=False, default="")
    is_flyer: bool = field(init=False, default=False)
    cat_src: str = field(init=False, default="rule")

    def __post_init__(self):
        cleaned = strip_savings(self.label)
        p = parse_price(cleaned)
        if p:
            self.price = float(f"{p[0].replace(',', '')}.{p[1]}")
            self.clearance = p[1] == "97"
        self.disc_pct = compute_discount_pct(self.label, self.price)
        self.end_date = parse_end_date(self.label)
        self.ident = (
            f"no:{self.item_no}"
            if self.item_no
            else (self.file_key or self.alt_key or self.url)
        )
        self.is_flyer = bool(
            FLYER_RE.search(self.label) or (self.file_key and "costco1" in self.file_key)
        )


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
    return (
        bool(url)
        and not url.startswith("data:")
        and not re.search(r"(placeholder|blank\.gif|1x1|lazy[-_]?load|spinner)", url, re.I)
    )


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
        if (
            attr.startswith("data-")
            and isinstance(val, str)
            and IMG_URL_RE.match(val.strip())
        ):
            out.append(val.strip())
    out.append(img.get("src"))
    return [u.strip() for u in out if isinstance(u, str) and usable(u.strip())]


def norm_text(s: str) -> str:
    return NON_ALNUM_RE.sub(" ", (s or "").lower()).strip()


def filename_key(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).stem.lower()
    name = FILE_SUFFIX_RE.sub("", name)
    key = NON_ALNUM_RE.sub("", name)
    return key if len(key) >= 5 and not GENERIC_NAME_RE.match(key) else ""


def pretty_name(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).stem
    name = re.sub(r"-\d{2,4}x\d{2,4}$", "", name)
    return re.sub(r"[-_]+", " ", name).strip().title()


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

    blob = " ".join(
        [img.get("alt") or "", img.get("title") or "", cap_text, Path(u.path).name]
    ).lower()

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
        label = alt if good_alt else pretty_name(url)
        alt_key = norm_text(alt) if good_alt else ""
        file_key = filename_key(url)
        item_no = find_item_no(alt, url)
        category = detect_category(label)
        it = Item(
            url=url,
            label=label,
            alt=alt,
            alt_key=alt_key,
            file_key=file_key,
            item_no=item_no,
            category=category,
        )
        if it.ident in seen:
            continue
        seen.add(it.ident)
        items.append(it)
    return items


def extract_items_from_soup(soup: BeautifulSoup, page_url: str):
    for sel in CONTAINER_SELECTORS:
        el = soup.select_one(sel)
        if el is not None and el.find("img"):
            items = extract_from(el, page_url)
            if items:
                return items
    return extract_from(soup, page_url)


def extract_location_from_soup(soup: BeautifulSoup, post_html: str) -> str:
    content = soup.select_one(".entry-content") or soup
    text = content.get_text(" ", strip=True)
    m = re.search(
        r"Location:\s*([A-Za-z0-9\s\(\),_-]+?)(?=\.\s|Hi\s|Hello\s|$)", text, re.I
    )
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
    sm = SequenceMatcher(None, "", "")

    def duplicate(it: Item) -> bool:
        if it.item_no:
            return it.item_no in mon_nos or (
                bool(it.file_key) and it.file_key in mon_files
            )
        if it.file_key and it.file_key in mon_files:
            return True
        if it.alt_key:
            if it.alt_key in mon_alt_set:
                return True
            if len(it.alt_key) >= 12:
                sm.set_seq1(it.alt_key)
                for m in mon_alts:
                    sm.set_seq2(m)
                    if (
                        sm.quick_ratio() >= FUZZY_THRESHOLD
                        and sm.ratio() >= FUZZY_THRESHOLD
                    ):
                        return True
        return False

    return [i for i in weekend_items if not duplicate(i)]


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


def update_price_db(prev_state, target_items, valid_range):
    db = dict(prev_state.get("price_db") or {})
    today_str = datetime.now(ZoneInfo("America/Vancouver")).strftime("%Y-%m-%d")
    date_label = valid_range[0].isoformat() if valid_range else today_str

    for i in target_items:
        ident = i.ident
        entry = db.get(
            ident,
            {
                "name": i.label,
                "item_no": i.item_no,
                "url": i.url,
                "cat": i.category,
                "history": [],
            },
        )
        entry["name"] = i.label
        entry["url"] = i.url
        entry["cat"] = i.category
        entry["learned"] = i.cat_src == "learned"
        if i.item_no:
            entry["item_no"] = i.item_no

        if i.price is not None:
            hist = entry.setdefault("history", [])
            if not hist:
                hist.append({"date": date_label, "price": i.price, "pct": i.disc_pct})
            elif hist[-1]["date"] == date_label:
                hist[-1]["price"] = i.price
                hist[-1]["pct"] = i.disc_pct
            elif hist[-1].get("price") != i.price or hist[-1].get("pct") != i.disc_pct:
                hist.append({"date": date_label, "price": i.price, "pct": i.disc_pct})
        db[ident] = entry
    return db


def is_all_time_low(it: Item, price_db: dict) -> bool:
    if it.price is None:
        return False
    entry = price_db.get(it.ident)
    if not entry:
        return False
    prices = [
        h["price"] for h in entry.get("history", []) if h.get("price") is not None
    ]
    return len(prices) >= 2 and max(prices) > it.price and it.price <= min(prices)


CATEGORY_KEYS = set(CATEGORY_RULES) | {"other"}
LEARN_STOP_WORDS = {
    "kirkland", "signature", "pack", "count", "instant", "savings", "expires", "the", "and",
    "with", "for", "size", "sizes", "pieces", "pcs", "item", "per", "each", "from", "new",
}


def load_category_overrides():
    """Optional hand-edited category_overrides.json: {"skus": {"1234567": "health"}, "words": {"mandu": "groceries"}}"""
    out = {"skus": {}, "words": {}}
    path = Path(os.getenv("CATEGORY_OVERRIDES", "category_overrides.json"))
    if not path.is_file():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[categories] could not read {path}: {exc!r}", file=sys.stderr)
        return out
    for kind in ("skus", "words"):
        for k, v in (data.get(kind) or {}).items():
            if v in CATEGORY_KEYS:
                out[kind][str(k).lower() if kind == "words" else str(k)] = v
    return out


def word_tokens(label: str) -> set:
    text = PAREN_RE.sub(" ", label or "").lower()
    toks = set()
    for t in re.findall(r"[a-z][a-z'\-]{2,}", text):
        t = t.strip("'-")
        if len(t) > 4 and t.endswith("s"):
            t = t[:-1]
        if len(t) >= 3 and t not in LEARN_STOP_WORDS:
            toks.add(t)
    return toks


def build_word_votes(price_db: dict) -> dict:
    votes = {}
    for entry in price_db.values():
        cat = entry.get("cat")
        if not cat or cat == "other" or entry.get("learned"):
            continue
        for t in word_tokens(entry.get("name", "")):
            votes.setdefault(t, Counter())[cat] += 1
    return votes


def learn_category(item: Item, price_db: dict, votes: dict):
    entry = price_db.get(f"no:{item.item_no}") if item.item_no else None
    if entry and entry.get("cat") not in (None, "other") and not entry.get("learned"):
        return entry["cat"], "sku"
    scores = Counter()
    for t in word_tokens(item.label):
        c = votes.get(t)
        if not c:
            continue
        total = sum(c.values())
        cat, n = c.most_common(1)[0]
        if total >= 2 and n / total >= 0.8:
            scores[cat] += total
    top = scores.most_common(2)
    if top and top[0][1] >= 3 and (len(top) == 1 or top[0][1] >= 2 * top[1][1]):
        return top[0][0], "learned"
    return None


def apply_categories(items, price_db: dict, overrides: dict):
    votes = build_word_votes(price_db)
    n_over = n_sku = n_learned = 0
    for it in items:
        sku_cat = overrides["skus"].get(it.item_no) if it.item_no else None
        if sku_cat:
            it.category, it.cat_src = sku_cat, "override"
            n_over += 1
            continue
        if it.category != "other":
            continue
        word_cat = next(
            (overrides["words"][t] for t in sorted(word_tokens(it.label)) if t in overrides["words"]),
            None,
        )
        if word_cat:
            it.category, it.cat_src = word_cat, "override"
            n_over += 1
            continue
        res = learn_category(it, price_db, votes)
        if res:
            it.category, it.cat_src = res
            if res[1] == "sku":
                n_sku += 1
            else:
                n_learned += 1
    print(
        f"[categories] overrides={n_over} reused-by-SKU={n_sku} learned-by-words={n_learned} "
        f"still-other={sum(1 for i in items if i.category == 'other')}"
    )


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
        with smtplib.SMTP_SSL(
            host, port, context=ssl.create_default_context(), timeout=30
        ) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg, to_addrs=recipients)
        print(f"[email] sent to {', '.join(recipients)}")
        return True
    except Exception as exc:
        print(f"[email] failed: {exc!r}", file=sys.stderr)
    return False


def digest_line(it: Item) -> str:
    bits = []
    if it.disc_pct:
        bits.append(f"{it.disc_pct}% off")
    if it.price is not None:
        bits.append(f"${it.price:.2f}")
    name = re.sub(r"\s+", " ", it.label).strip()[:90]
    return f"- {name}" + (f" ({', '.join(bits)})" if bits else "")


def build_digest(items, exclusive, is_weekend_mode):
    sections = []
    with_disc = sorted(
        (i for i in items if i.disc_pct), key=lambda i: i.disc_pct, reverse=True
    )
    if with_disc:
        sections.append(
            "BIGGEST DISCOUNTS\n" + "\n".join(digest_line(i) for i in with_disc[:10])
        )

    if is_weekend_mode and exclusive:
        top_new = sorted(exclusive, key=lambda i: i.disc_pct or 0, reverse=True)[:5]
        sections.append(
            "NEW THIS WEEKEND\n" + "\n".join(digest_line(i) for i in top_new)
        )

    clearance = [i for i in items if i.clearance]
    if clearance:
        shown = clearance[:5]
        extra = len(clearance) - len(shown)
        text = "CLEARANCE FINDS (.97 prices)\n" + "\n".join(
            digest_line(i) for i in shown
        )
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

    best = max((i.disc_pct or 0 for i in items), default=0)
    best_txt = f" | top deal {best}% off" if best else ""
    if is_weekend_mode:
        subj = f"CocoWest: {exclusive_count} new weekend deals{best_txt}"
    else:
        subj = f"CocoWest: Monday Sales Post ({items_count} items){best_txt}"
    send_email(subj, "\n".join(lines))


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1b8a3a">
<title>__PAGE_TITLE__ (__TOTAL__ items)</title>
<style>
  :root {
    --bg:#f4f5f7; --fg:#1a1a1a; --card:#ffffff; --muted:#666; --line:#e2e2e2;
    --accent:#1b8a3a; --on-accent:#ffffff; --bar:rgba(255,255,255,.95);
    --highlight:#e6f4ea; --highlight-border:#34a853; --danger:#dc3545; --warn:#fd7e14;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#121212; --fg:#eeeeee; --card:#1e1e1e; --muted:#9a9a9a; --line:#333;
            --accent:#3ddc6a; --on-accent:#08210f; --bar:rgba(18,18,18,.95);
            --highlight:#132618; --highlight-border:#28a745; }
  }
  * { box-sizing:border-box; }
  [hidden] { display:none !important; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--fg); font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
         padding-bottom:4.5rem; padding-left:env(safe-area-inset-left,0px); padding-right:env(safe-area-inset-right,0px); }

  .sticky-header {
    position:sticky; top:0; z-index:10; background:var(--bar);
    -webkit-backdrop-filter:blur(8px); backdrop-filter:blur(8px);
    border-bottom:1px solid var(--line); padding-top:env(safe-area-inset-top, 0px);
  }
  .bar { padding:.55rem .8rem .35rem; }
  .bar-row { max-width:1200px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; gap:.45rem; }
  .title { font-size:1.02rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .header-btns { display:flex; align-items:center; gap:.35rem; }
  .badge-btn { background:var(--accent); color:var(--on-accent); border:none; border-radius:999px; padding:.32rem .75rem;
               font-size:.82rem; font-weight:700; cursor:pointer; white-space:nowrap; }
  .badge-btn.gray { background:#6c757d; color:#fff; }
  .badge-btn.next-mode { background:#0d6efd; color:#fff; }
  .badge-btn.active { background:var(--danger); color:#fff; }

  .search { max-width:1200px; margin:.45rem auto 0; display:flex; gap:.45rem; }
  .search input { flex:1; min-width:0; padding:.55rem .75rem; font-size:16px; border:1px solid var(--line); border-radius:10px;
                  background:var(--card); color:var(--fg); }

  .quick-add { max-width:1200px; margin:.35rem auto 0; padding:.35rem .6rem; background:var(--highlight);
               border:1px solid var(--highlight-border); border-radius:10px; display:flex; flex-wrap:wrap;
               align-items:center; justify-content:space-between; gap:.4rem; font-size:.8rem; }
  .quick-add-actions { display:flex; gap:.35rem; flex-wrap:wrap; }

  .toolbar-row { max-width:1200px; margin:0 auto; padding:.1rem .8rem .45rem; display:flex; align-items:center; gap:.4rem; }
  .filters-panel { border-top:1px solid var(--line); max-height:42vh; overflow-y:auto; }
  .toolbar { max-width:1200px; margin:0 auto; padding:.35rem .8rem .5rem; display:flex; flex-wrap:wrap; align-items:center; gap:.32rem; }
  .chip { border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:999px; padding:.24rem .6rem;
          font-size:.78rem; font-weight:600; cursor:pointer; display:inline-flex; align-items:center; gap:.22rem; white-space:nowrap; }
  .chip.active { background:var(--accent); color:var(--on-accent); border-color:var(--accent); }
  .chip.warn-active { background:var(--warn); color:#fff; border-color:var(--warn); }
  .chip.next-active { background:#0d6efd; color:#fff; border-color:#0d6efd; }
  .chip.danger { color:var(--danger); border-color:var(--danger); }
  .sort-select { border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:999px;
                 padding:.24rem .5rem; font-size:.78rem; font-weight:600; }
  .cols-toggle { display:none; align-items:center; gap:.3rem; margin-left:auto; font-size:.78rem; color:var(--muted); }
  .seg { display:inline-flex; border:1px solid var(--line); border-radius:999px; overflow:hidden; }
  .seg button { border:none; background:var(--card); color:var(--fg); padding:.25rem .65rem; font-size:.8rem; font-weight:700; cursor:pointer; }
  .seg button.active { background:var(--accent); color:var(--on-accent); }

  .list-bar { max-width:1200px; margin:0 auto; padding:.55rem .8rem .2rem; display:flex; flex-wrap:wrap; align-items:center; gap:.35rem; }
  .list-sum { font-size:.82rem; font-weight:700; margin-right:auto; }

  .meta { max-width:1200px; margin:.5rem auto .35rem; padding:0 .8rem; color:var(--muted); font-size:.78rem; line-height:1.45; }
  .meta a { color:inherit; }

  .grid { max-width:1200px; margin:.5rem auto; padding:0 .8rem; display:grid; gap:.75rem; grid-template-columns:repeat(auto-fill,minmax(215px,1fr)); }
  .grid.c1 { grid-template-columns:minmax(0,1fr); }
  .grid.c2 { grid-template-columns:repeat(2,minmax(0,1fr)); }

  /* Compact No-Image Checklist Mode */
  .grid.no-img { grid-template-columns:minmax(0,1fr) !important; gap:.4rem; }
  .grid.no-img .card { flex-direction:column; align-items:stretch; padding:.3rem 0; border-radius:10px; }
  .grid.no-img .card > a, .grid.no-img .new-tag, .grid.no-img .tags-right { display:none !important; }
  .grid.no-img .card figcaption { padding:.5rem .8rem .2rem; font-size:.88rem; font-weight:600; width:100%; }
  .grid.no-img .card-footer { border-top:1px solid var(--line); padding:.4rem .8rem .5rem; background:rgba(0,0,0,.02); width:100%; }

  /* Normal Grid Cards */
  .card { margin:0; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; position:relative;
          display:flex; flex-direction:column; justify-content:space-between; }
  .card.is-new { border:2px solid var(--highlight-border); background:var(--highlight); }
  .card.is-expired img, .card.is-expired figcaption { opacity:.5; }
  .card.is-got figcaption { text-decoration:line-through; opacity:.55; }
  .card.is-got img { opacity:.45; filter:grayscale(80%); }

  /* Badges overlaid on the item photo */
  .new-tag, .tags-right { position:absolute; z-index:2; pointer-events:none; }
  .new-tag { top:8px; left:8px; background:var(--highlight-border); color:#fff; font-size:.72rem; font-weight:800;
             padding:2px 8px; border-radius:999px; box-shadow:0 1px 4px rgba(0,0,0,.4); white-space:nowrap; }
  .new-tag.carry { background:#0d6efd; }
  .new-tag.custom { background:#6f42c1; }
  .card.is-carryover .new-tag { position:static; align-self:flex-start; margin:.55rem 0 0 .7rem; }
  .tags-right { top:8px; right:8px; display:flex; flex-direction:column; align-items:flex-end; gap:4px; }
  .disc-tag, .atl-tag, .clr-tag { color:#fff; font-size:.74rem; font-weight:800; line-height:1.3; padding:2px 8px;
                                  border-radius:999px; box-shadow:0 1px 4px rgba(0,0,0,.4); white-space:nowrap; }
  .disc-tag { background:var(--danger); }
  .atl-tag { background:#f76707; }
  .clr-tag { background:#7048e8; }

  .card a { display:block; width:100%; }
  .card img { width:100%; aspect-ratio:1/1; object-fit:contain; display:block; background:#fff; }
  .card figcaption { padding:.6rem .8rem; font-size:.86rem; line-height:1.35; width:100%; overflow-wrap:anywhere; cursor:pointer; flex-grow:1; }
  .card-footer { padding:.5rem .8rem .6rem; display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:.4rem;
                 border-top:1px solid var(--line); font-size:.78rem; width:100%; }

  .card-actions { display:flex; align-items:center; gap:.3rem; flex-wrap:wrap; }
  .shop-label { display:inline-flex; align-items:center; gap:.3rem; cursor:pointer; font-weight:600; }
  .shop-label input { width:1.05rem; height:1.05rem; accent-color:var(--accent); cursor:pointer; margin:0; }
  .mini-btn { border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:6px; padding:1px 6px;
              font-size:.7rem; font-weight:700; cursor:pointer; line-height:1.35; }
  .mini-btn.active { background:#0d6efd; color:#fff; border-color:#0d6efd; }
  .mini-btn.got-active { background:var(--accent); color:var(--on-accent); border-color:var(--accent); }

  .foot-right { display:flex; align-items:center; gap:.28rem; flex-wrap:wrap; justify-content:flex-end; margin-left:auto; }
  .sku-badge { font-size:.66rem; font-weight:700; padding:2px 5px; border-radius:4px; background:var(--line); color:var(--fg); cursor:pointer; }
  .repeat-tag { font-size:.66rem; font-weight:600; padding:2px 6px; border-radius:999px; border:1px solid var(--line); color:var(--muted); }
  .end-badge { font-size:.7rem; font-weight:700; padding:2px 7px; border-radius:999px; white-space:nowrap; }
  .end-badge.today { background:#dc3545; color:#fff; }
  .end-badge.tomorrow { background:#fd7e14; color:#fff; }
  .end-badge.week { background:#ffc107; color:#1a1a1a; }
  .end-badge.later { background:transparent; color:var(--muted); border:1px solid var(--line); font-weight:600; }
  .end-badge.expired { background:var(--line); color:var(--muted); text-decoration:line-through; }

  .empty { padding:1.5rem 1rem; text-align:center; color:var(--muted); grid-column:1/-1; }
  .db-results { grid-column:1/-1; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:.9rem; margin-top:.3rem; }
  .db-results h4 { margin:0 0 .6rem; font-size:.9rem; }
  .db-row { display:flex; align-items:center; justify-content:space-between; gap:.6rem; padding:.45rem 0; border-top:1px solid var(--line); font-size:.82rem; }
  .db-row img { width:44px; height:44px; object-fit:contain; border-radius:6px; background:#fff; border:1px solid var(--line); flex-shrink:0; }

  .modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,.6); backdrop-filter:blur(4px); z-index:100; display:flex; align-items:center; justify-content:center; padding:1rem; }
  .modal { background:var(--card); color:var(--fg); border-radius:14px; max-width:450px; width:100%; max-height:85vh; overflow-y:auto; padding:1.2rem; box-shadow:0 8px 24px rgba(0,0,0,.3); position:relative; }
  .modal-close { position:absolute; top:12px; right:12px; border:none; background:none; font-size:1.4rem; cursor:pointer; color:var(--muted); }
  .cat-row { display:flex; align-items:center; gap:.5rem; font-size:.85rem; font-weight:600; margin:.2rem 0 .4rem; }
  .cat-row select { flex:1; min-width:0; border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:8px; padding:.3rem .45rem; font-size:.85rem; }
  .hist-list { list-style:none; padding:0; margin:.8rem 0 0; }
  .hist-item { display:flex; justify-content:space-between; padding:.5rem 0; border-bottom:1px solid var(--line); font-size:.85rem; }

  .top-btn { position:fixed; right:calc(.8rem + env(safe-area-inset-right,0px)); bottom:calc(.8rem + env(safe-area-inset-bottom,0px));
             width:2.8rem; height:2.8rem; border-radius:50%; border:none; background:var(--accent); color:var(--on-accent);
             font-size:1.4rem; font-weight:700; line-height:1; cursor:pointer; box-shadow:0 2px 8px rgba(0,0,0,.35);
             z-index:20; opacity:0; pointer-events:none; transition:opacity .2s; }
  .top-btn.show { opacity:1; pointer-events:auto; }

  @media (max-width:640px) {
    .cols-toggle { display:flex; }
    .toolbar { padding:.25rem .6rem .45rem; gap:.24rem; }
    .toolbar-row { padding:.1rem .6rem .4rem; }
    .list-bar { padding:.45rem .6rem .15rem; gap:.28rem; }
    .chip { font-size:.71rem; padding:.2rem .48rem; }
    .sort-select { font-size:.71rem; padding:.2rem .4rem; }
    .grid { padding:0 .5rem; gap:.5rem; }
    .grid.c2 .card figcaption { font-size:.73rem; padding:.38rem .45rem; }
    .grid.c2 .new-tag, .grid.c2 .disc-tag, .grid.c2 .atl-tag, .grid.c2 .clr-tag { font-size:.64rem; padding:1px 6px; }
    .grid.c2 .new-tag { top:5px; left:5px; }
    .grid.c2 .tags-right { top:5px; right:5px; gap:3px; }
    .grid.c2 .card-footer { padding:.32rem .45rem .45rem; font-size:.71rem; }
    .title { font-size:.92rem; }
    .meta { padding:0 .6rem; }
  }
</style>
</head>
<body>
<div class="sticky-header">
  <header class="bar">
    <div class="bar-row">
      <div class="title">&#128717;&#65039; __PAGE_TITLE__</div>
      <div class="header-btns">
        <button class="badge-btn next-mode" id="next-toggle" type="button" title="View items saved for next week">&#128204; Next Wk (0)</button>
        <button class="badge-btn" id="list-toggle" type="button">&#128722; Cart (0)</button>
      </div>
    </div>
    <div class="search">
      <input id="q" type="search" placeholder="Search items, SKU, or type custom item (e.g. Milk $5.99)..." autocomplete="off">
      <button class="badge-btn gray" id="reset-btn" type="button">Reset</button>
    </div>
    <div class="quick-add" id="quick-add" hidden>
      <span id="quick-add-label"></span>
      <div class="quick-add-actions">
        <button class="chip active" id="add-custom-cart" type="button">+ Add to Cart</button>
        <button class="chip next-active" id="add-custom-next" type="button">&#128204; Save for Next Wk</button>
        <button class="chip" id="search-hist-btn" type="button">&#128269; Search Past Catalog</button>
      </div>
    </div>
  </header>
  <div class="toolbar-row">
    <button class="chip" id="filters-toggle" type="button" aria-expanded="false" aria-controls="filters-panel">&#9776; Filters<span id="filters-count"></span></button>
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
      <span>Cols</span>
      <span class="seg">
        <button type="button" data-cols="1" aria-pressed="false">1</button>
        <button type="button" data-cols="2" aria-pressed="true">2</button>
      </span>
    </div>
  </div>
  <div class="toolbar filters-panel" id="filters-panel" hidden>
    __NEW_BTN__
    <button class="chip" id="sale-toggle" type="button" aria-pressed="false">&#128293; Sale (__SALE_COUNT__)</button>
    __ATL_BTN__
    <button class="chip" id="clr-toggle" type="button" aria-pressed="false">&#127991;&#65039; .97 (__CLR_COUNT__)</button>
    <button class="chip" id="week-cycle" type="button" title="Cycle: All -> Ends this week -> Hide ending this week">&#9200; Ends This Wk (0)</button>
    <button class="chip" id="exp-toggle" type="button" aria-pressed="false" hidden>&#128584; Hide Expired (0)</button>
    __REPEAT_BTN__
    __CAT_BTNS__
  </div>
</div>
  <div class="list-bar" id="list-bar" hidden>
    <span class="list-sum" id="list-sum"></span>
    <button class="chip" id="img-toggle" type="button" title="Toggle compact text checklist mode">&#128444;&#65039; Compact Rows</button>
    <button class="chip" id="tax-toggle" type="button" title="Rough BC GST/PST estimate (basic groceries 0%)">+ BC Tax</button>
    <button class="chip" id="promote-next" type="button" hidden>&#128722; Move All to Cart</button>
    <button class="chip" id="clear-got" type="button" hidden>&#10003; Clear Got-It</button>
    <button class="chip" id="copy-list" type="button">&#128203; Copy</button>
    <button class="chip" id="share-list" type="button">&#128279; Share</button>
    <button class="chip danger" id="clear-list" type="button">Clear</button>
  </div>
<div class="meta">
  &#128205; Store Location: <b>__LOCATION__</b><br>
  __VALID_LINE__
  Showing <b id="shown-count">__TOTAL__</b> of <b>__TOTAL__</b> items &middot; updated __STAMP__<br>
  <span id="cat-fixes" hidden>&#127991;&#65039; <b id="cat-fixes-n">0</b> category fixes saved on this device &middot; <button type="button" class="mini-btn" id="cat-fixes-copy">Copy as overrides</button><br></span>
  Source: <a href="__SOURCE_URL__">__SOURCE_NAME__</a>
</div>
<main class="grid c2" id="grid">
__CARDS__
<div class="db-results" id="db-results" hidden></div>
<p class="empty" id="no-match" hidden></p>
</main>

<div class="modal-overlay" id="sku-modal" hidden>
  <div class="modal">
    <button class="modal-close" id="modal-close">&times;</button>
    <h3 id="modal-title" style="margin-top:0;">Price History</h3>
    <div id="modal-cat-row"></div>
    <div id="modal-body">Loading SKU history...</div>
  </div>
</div>

<button class="top-btn" id="top-btn" type="button" aria-label="Back to top" title="Back to top">&#8593;</button>
<script>
window.__PAGE_HIST__ = __EMBEDDED_HIST__;
(function () {
  var STORE_KEY = 'cocowest_saved_v2';
  var LEGACY_LIST_KEY = 'cocowest_shopping_list';
  var COLS_KEY = 'cocowest_cols';
  var HIDE_EXP_KEY = 'cocowest_hide_expired';
  var NO_IMG_KEY = 'cocowest_no_img';
  var TAX_KEY = 'cocowest_bc_tax';
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var NL = String.fromCharCode(10);

  // Rough BC tax estimate (GST 5% + PST 7%). Basic groceries 0%; snack/candy/pop/prepared food GST only;
  // vitamins/OTC and kids' clothing GST only (PST exempt); everything else 12%.
  var SNACK_RE = /\b(chips?|crisps|doritos|lays|ruffles|pringles|cheetos|tostitos|popcorn|pretzels?|candy|candies|gumm(?:y|ies)|chocolates?|licorice|marshmallows?|gum|granola bars?|protein bars?|cereal bars?|snack bars?|clif|rxbar|kind bars?|nature valley|trail mix|snack mix|soda|pop|coke|coca-?cola|pepsi|sprite|7up|ginger ale|root beer|dr\.? pepper|sparkling|carbonated|seltzer|kombucha|energy drinks?|red bull|monster|celsius|alani|bubly|perrier|pellegrino|popsicles?|ice cream bars?|rotisserie|sushi|sandwich(?:es)?|party tray|platter)\b/i;
  var PERSONAL_RE = /\b(shampoo|conditioner|razors?|shav(?:e|ing)|deodorant|antiperspirant|sunscreen|sunblock|lotion|moisturizer|cleanser|body wash|face wash|soap|(?:face|body|hand|eye|night) cream|lip balm|toothpaste|mouthwash|floss|toothbrush|cosmetics?|makeup|perfume|hair|nail)\b/i;
  var KIDS_RE = /\b(kids?|child(?:ren)?|boys?|girls?|youth|toddler|infant|baby)\b/i;
  var DIAPER_RE = /\bdiapers?\b/i;

  function taxRate(name, cat) {
    var n = name || '';
    if (DIAPER_RE.test(n)) return 0.05;
    if (cat === 'groceries') return SNACK_RE.test(n) ? 0.05 : 0;
    if (cat === 'health') return PERSONAL_RE.test(n) ? 0.12 : 0.05;
    if (cat === 'clothing') return KIDS_RE.test(n) ? 0.05 : 0.12;
    return 0.12;
  }

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

  function hashStr(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) { h = ((h << 5) + h + s.charCodeAt(i)) | 0; }
    return (h >>> 0).toString(36);
  }

  function esc(v) {
    return String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var q = document.getElementById('q');
  var quickAdd = document.getElementById('quick-add');
  var quickAddLabel = document.getElementById('quick-add-label');
  var addCustomCartBtn = document.getElementById('add-custom-cart');
  var addCustomNextBtn = document.getElementById('add-custom-next');
  var searchHistBtn = document.getElementById('search-hist-btn');
  var dbResults = document.getElementById('db-results');

  var grid = document.getElementById('grid');
  var listBtn = document.getElementById('list-toggle');
  var nextBtn = document.getElementById('next-toggle');
  var newBtn = document.getElementById('new-toggle');
  var saleBtn = document.getElementById('sale-toggle');
  var atlBtn = document.getElementById('atl-toggle');
  var clrBtn = document.getElementById('clr-toggle');
  var weekCycleBtn = document.getElementById('week-cycle');
  var expBtn = document.getElementById('exp-toggle');
  var repBtn = document.getElementById('rep-toggle');
  var sortSel = document.getElementById('sort');
  var catBtns = Array.prototype.slice.call(document.querySelectorAll('button[data-cat]'));
  var filtersToggle = document.getElementById('filters-toggle');
  var filtersPanel = document.getElementById('filters-panel');
  var filtersCount = document.getElementById('filters-count');

  var listBar = document.getElementById('list-bar');
  var listSum = document.getElementById('list-sum');
  var imgToggleBtn = document.getElementById('img-toggle');
  var taxToggleBtn = document.getElementById('tax-toggle');
  var promoteNextBtn = document.getElementById('promote-next');
  var clearGotBtn = document.getElementById('clear-got');
  var copyBtn = document.getElementById('copy-list');
  var shareBtn = document.getElementById('share-list');
  var clearBtn = document.getElementById('clear-list');
  var resetBtn = document.getElementById('reset-btn');
  var topBtn = document.getElementById('top-btn');
  var shownEl = document.getElementById('shown-count');
  var emptyMsg = document.getElementById('no-match');
  var colBtns = Array.prototype.slice.call(document.querySelectorAll('[data-cols]'));

  var modal = document.getElementById('sku-modal');
  var modalClose = document.getElementById('modal-close');
  var modalTitle = document.getElementById('modal-title');
  var modalBody = document.getElementById('modal-body');

  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var origOrder = cards.slice();
  var cardMetaMap = {};
  var hashToIdent = {};

  var OV_KEY = 'cocowest_cat_overrides';
  var CATS = [['groceries', 'Groceries'], ['household', 'Household'], ['health', 'Health'], ['home', 'Home'],
              ['appliances', 'Appliances'], ['electronics', 'Electronics'], ['clothing', 'Clothing'], ['other', 'Other']];
  var CAT_LABEL = {};
  CATS.forEach(function (c) { CAT_LABEL[c[0]] = c[1]; });
  var overrides = load(OV_KEY, {});
  if (!overrides || typeof overrides !== 'object' || Array.isArray(overrides)) overrides = {};
  var modalCatRow = document.getElementById('modal-cat-row');
  var catFixesEl = document.getElementById('cat-fixes');
  var catFixesN = document.getElementById('cat-fixes-n');
  var catFixesCopy = document.getElementById('cat-fixes-copy');

  function ovKey(el) {
    var sku = el.getAttribute('data-sku');
    return sku ? 'sku:' + sku : 'id:' + el.getAttribute('data-ident');
  }

  function num(el, attr) {
    var v = parseFloat(el.getAttribute(attr));
    return isNaN(v) ? null : v;
  }

  // Load saved items map: { [ident]: { status: 'cart'|'next', qty: 1, got: false, name, url, sku, cat, price, disc, end, custom } }
  var savedMap = load(STORE_KEY, null);
  if (!savedMap || typeof savedMap !== 'object' || Array.isArray(savedMap)) {
    savedMap = {};
    var legacyUrls = load(LEGACY_LIST_KEY, []);
    if (Array.isArray(legacyUrls) && legacyUrls.length) {
      cards.forEach(function (el) {
        if (legacyUrls.indexOf(el.getAttribute('data-url')) !== -1) {
          var id = el.getAttribute('data-ident');
          savedMap[id] = extractCardData(el, 'cart', 1, false);
        }
      });
    }
    save(STORE_KEY, savedMap);
  }

  function extractCardData(el, status, qty, got) {
    return {
      status: status || 'cart',
      qty: qty || 1,
      got: !!got,
      name: el.getAttribute('data-name') || '',
      url: el.getAttribute('data-url') || '',
      sku: el.getAttribute('data-sku') || '',
      cat: el.getAttribute('data-cat') || 'other',
      price: num(el, 'data-price'),
      disc: num(el, 'data-disc'),
      end: el.getAttribute('data-end') || '',
      custom: el.getAttribute('data-custom') === '1'
    };
  }

  var state = {
    viewMode: 'all', // 'all' | 'cart' | 'next'
    newOnly: false, saleOnly: false, atlOnly: false, clrOnly: false,
    weekMode: 0, // 0 = off, 1 = only ending this week, 2 = hide ending this week
    hideExp: !!load(HIDE_EXP_KEY, false), hideRep: false,
    noImg: !!load(NO_IMG_KEY, false), bcTax: !!load(TAX_KEY, false),
    activeCat: '', sort: 'default'
  };

  var now = new Date();
  var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

  function buildCarryoverCard(id, item) {
    var fig = document.createElement('figure');
    fig.className = 'card is-carryover';
    fig.setAttribute('data-ident', id);
    fig.setAttribute('data-url', item.url || '');
    fig.setAttribute('data-name', item.name || id);
    fig.setAttribute('data-t', ((item.name || '') + ' ' + (item.sku || '')).toLowerCase());
    fig.setAttribute('data-sku', item.sku || '');
    fig.setAttribute('data-cat', item.cat || 'other');
    fig.setAttribute('data-idx', String(90000 + origOrder.length));
    if (item.custom) fig.setAttribute('data-custom', '1');
    if (item.price !== null && item.price !== undefined) fig.setAttribute('data-price', Number(item.price).toFixed(2));
    if (item.disc) fig.setAttribute('data-disc', String(item.disc));
    if (item.end) fig.setAttribute('data-end', item.end);

    var tagHtml = item.custom
      ? '<div class="new-tag custom">&#9998; Custom</div>'
      : '<div class="new-tag carry">&#128204; Saved Item</div>';
    var imgPart = item.url
      ? '<a href="' + esc(item.url) + '" target="_blank" rel="noopener"><img src="' + esc(item.url) + '" alt="" loading="lazy" referrerpolicy="no-referrer"></a>'
      : '';
    var skuPart = item.sku ? '<span class="sku-badge">#' + esc(item.sku) + '</span>' : '';
    
    // Ensure name and price display nicely in captions and compact rows
    var displayName = esc(item.name || id);
    var pTxt = (item.price !== null && item.price !== undefined && displayName.indexOf('$') === -1)
      ? ' ($' + Number(item.price).toFixed(2) + ')'
      : '';

    fig.innerHTML =
      tagHtml + imgPart +
      '<figcaption>' + displayName + pTxt + '</figcaption>' +
      '<div class="card-footer">' +
        '<div class="card-actions">' +
          '<label class="shop-label"><input type="checkbox" class="shop-check" data-ident="' + esc(id) + '"> Cart</label>' +
          '<button type="button" class="mini-btn next-item-btn" data-ident="' + esc(id) + '" title="Save for next week">&#128204; Next</button>' +
          '<button type="button" class="mini-btn qty-btn" data-ident="' + esc(id) + '" hidden>&times;1</button>' +
          '<button type="button" class="mini-btn got-btn" data-ident="' + esc(id) + '" hidden>&#10003;</button>' +
        '</div>' +
        '<span class="foot-right">' + skuPart + '<span class="end-badge" hidden></span></span>' +
      '</div>';

    grid.insertBefore(fig, dbResults);
    cards.push(fig);
    origOrder.push(fig);
    initCard(fig);
    return fig;
  }

  function syncCardControls(el, id) {
    var entry = savedMap[id];
    var chk = el.querySelector('.shop-check');
    var nxt = el.querySelector('.next-item-btn');
    var qtyBtn = el.querySelector('.qty-btn');
    var gotBtn = el.querySelector('.got-btn');
    var inCart = !!(entry && entry.status === 'cart');
    var inNext = !!(entry && entry.status === 'next');

    if (chk) chk.checked = inCart;
    if (nxt) nxt.classList.toggle('active', inNext);
    if (qtyBtn) {
      qtyBtn.hidden = !entry;
      qtyBtn.innerHTML = '&times;' + (entry ? (entry.qty || 1) : 1);
    }
    if (gotBtn) {
      gotBtn.hidden = !inCart;
      gotBtn.classList.toggle('got-active', !!(entry && entry.got));
    }
    el.classList.toggle('is-got', !!(inCart && entry && entry.got));
  }

  var fullPriceDb = null;
  function fetchFullPriceDb(cb) {
    if (fullPriceDb) return cb(fullPriceDb);
    fetch('state.json?t=' + Date.now()).then(function (r) { return r.json(); }).then(function (d) {
      fullPriceDb = d.price_db || {};
      cb(fullPriceDb);
    }).catch(function () { cb({}); });
  }

  function updateFixesNote() {
    var n = Object.keys(overrides).length;
    catFixesEl.hidden = n === 0;
    catFixesN.textContent = n;
  }

  function setCategory(id, val) {
    var m = cardMetaMap[id];
    if (!m) return;
    var key = ovKey(m.el);
    if (val) overrides[key] = val; else delete overrides[key];
    save(OV_KEY, overrides);
    var cat = val || m.autoCat;
    m.cat = cat;
    m.el.setAttribute('data-cat', cat);
    if (savedMap[id]) { savedMap[id].cat = cat; save(STORE_KEY, savedMap); }
    updateFixesNote();
    render();
  }

  function renderCatRow(id) {
    var m = cardMetaMap[id];
    if (!m) { modalCatRow.innerHTML = ''; return; }
    var cur = overrides[ovKey(m.el)] || '';
    var html = '<label class="cat-row">Category <select id="modal-cat">' +
      '<option value="">Auto (' + esc(CAT_LABEL[m.autoCat] || m.autoCat) + ')</option>';
    CATS.forEach(function (c) {
      html += '<option value="' + c[0] + '"' + (cur === c[0] ? ' selected' : '') + '>' + c[1] + '</option>';
    });
    html += '</select></label>';
    modalCatRow.innerHTML = html;
    document.getElementById('modal-cat').addEventListener('change', function (e) { setCategory(id, e.target.value); });
  }

  function showHistoryModal(ident, name, sku) {
    modalTitle.textContent = name;
    modal.hidden = false;
    renderCatRow(ident);
    var ext = sku ? '<p style="font-size:.8rem;margin-top:.8rem;text-align:right;"><a href="https://www.cocowest.ca/?s=' + encodeURIComponent(sku) + '" target="_blank" rel="noopener" style="color:var(--accent);">Search #' + sku + ' on CocoWest &rarr;</a></p>' : '';

    function renderHist(histArr) {
      if (!histArr || !histArr.length) {
        modalBody.innerHTML = '<p style="color:var(--muted);font-size:.85rem;">No historical price data recorded yet for this item.</p>' + ext;
        return;
      }
      var low = Math.min.apply(null, histArr.map(function (h) { return Number(h.price); }));
      var html = '<p style="color:var(--muted);font-size:.82rem;">Recorded Price History (Lowest: <b>$' + low.toFixed(2) + '</b>):</p><ul class="hist-list">';
      histArr.slice().reverse().forEach(function (h) {
        var isLow = Number(h.price) <= low && histArr.length > 1;
        html += '<li class="hist-item"><span><b>' + h.date + '</b></span><span>$' + Number(h.price).toFixed(2) +
                (h.pct ? ' (' + h.pct + '% off)' : '') + (isLow ? ' &#128293;' : '') + '</span></li>';
      });
      html += '</ul>' + ext;
      modalBody.innerHTML = html;
    }

    if (window.__PAGE_HIST__ && window.__PAGE_HIST__[ident]) {
      renderHist(window.__PAGE_HIST__[ident]);
    } else {
      modalBody.innerHTML = '<p style="color:var(--muted);font-size:.85rem;">Loading recorded price history...</p>';
      fetchFullPriceDb(function (db) {
        renderHist(db[ident] && db[ident].history ? db[ident].history : []);
      });
    }
  }

  function initCard(el) {
    var id = el.getAttribute('data-ident');
    var url = el.getAttribute('data-url') || '';
    var name = el.getAttribute('data-name') || '';
    var sku = el.getAttribute('data-sku') || '';
    var autoCat = el.getAttribute('data-cat') || 'other';
    var ovCat = overrides[ovKey(el)];
    if (ovCat && CAT_LABEL[ovCat]) el.setAttribute('data-cat', ovCat);
    var sEnd = el.getAttribute('data-end');
    var badge = el.querySelector('.end-badge');
    if (sEnd && badge) {
      var r = classifyEnd(sEnd, today);
      badge.className = 'end-badge ' + r.cls;
      badge.textContent = r.text;
      badge.hidden = false;
      if (r.expired) el.classList.add('is-expired');
      if (r.inWeek) el.setAttribute('data-week', '1');
    }

    hashToIdent[hashStr(id)] = id;
    if (url) hashToIdent[hashStr(url)] = id;

    // Cache metadata for fast keystroke filtering
    cardMetaMap[id] = {
      el: el,
      t: el.getAttribute('data-t') || '',
      cat: el.getAttribute('data-cat') || 'other',
      autoCat: autoCat,
      price: num(el, 'data-price'),
      disc: num(el, 'data-disc'),
      end: sEnd || '',
      idx: Number(el.getAttribute('data-idx') || 0),
      isNew: el.classList.contains('is-new'),
      isSale: el.classList.contains('is-sale'),
      isAtl: el.classList.contains('is-atl'),
      isClr: el.classList.contains('is-clr'),
      isExp: el.classList.contains('is-expired'),
      isWeek: el.getAttribute('data-week') === '1',
      isSeen: el.getAttribute('data-seen') === '1',
      isFlyer: el.getAttribute('data-flyer') === '1',
      isCarry: el.classList.contains('is-carryover')
    };

    // Update saved entry with latest flyer details if present on page
    if (savedMap[id] && !el.classList.contains('is-carryover')) {
      savedMap[id] = extractCardData(el, savedMap[id].status, savedMap[id].qty, savedMap[id].got);
    }

    var figCap = el.querySelector('figcaption');
    var skuBadge = el.querySelector('.sku-badge');
    if (figCap) figCap.addEventListener('click', function () { showHistoryModal(id, name, sku); });
    if (skuBadge) skuBadge.addEventListener('click', function () { showHistoryModal(id, name, sku); });

    var chk = el.querySelector('.shop-check');
    var nxt = el.querySelector('.next-item-btn');
    var qtyBtn = el.querySelector('.qty-btn');
    var gotBtn = el.querySelector('.got-btn');

    if (chk) {
      chk.addEventListener('change', function () {
        if (chk.checked) {
          var prevQty = savedMap[id] ? savedMap[id].qty : 1;
          savedMap[id] = extractCardData(el, 'cart', prevQty, false);
        } else {
          delete savedMap[id];
        }
        save(STORE_KEY, savedMap);
        syncCardControls(el, id);
        render();
      });
    }
    if (nxt) {
      nxt.addEventListener('click', function () {
        if (savedMap[id] && savedMap[id].status === 'next') {
          delete savedMap[id];
        } else {
          var prevQty = savedMap[id] ? savedMap[id].qty : 1;
          savedMap[id] = extractCardData(el, 'next', prevQty, false);
        }
        save(STORE_KEY, savedMap);
        syncCardControls(el, id);
        render();
      });
    }
    if (qtyBtn) {
      qtyBtn.addEventListener('click', function () {
        if (!savedMap[id]) return;
        savedMap[id].qty = ((savedMap[id].qty || 1) % 5) + 1;
        save(STORE_KEY, savedMap);
        syncCardControls(el, id);
        render();
      });
    }
    if (gotBtn) {
      gotBtn.addEventListener('click', function () {
        if (!savedMap[id]) return;
        savedMap[id].got = !savedMap[id].got;
        save(STORE_KEY, savedMap);
        syncCardControls(el, id);
        render();
      });
    }
    syncCardControls(el, id);
  }

  cards.forEach(initCard);

  // Inject carryover/custom cards from savedMap that are not in this post
  Object.keys(savedMap).forEach(function (id) {
    if (!cardMetaMap[id]) {
      buildCarryoverCard(id, savedMap[id]);
    }
  });
  save(STORE_KEY, savedMap);

  modalClose.addEventListener('click', function () { modal.hidden = true; });
  modal.addEventListener('click', function (e) { if (e.target === modal) modal.hidden = true; });

  var weekCount = 0, expCount = 0;
  origOrder.forEach(function (el) {
    var m = cardMetaMap[el.getAttribute('data-ident')];
    if (!m || m.isCarry) return;
    if (m.isWeek) weekCount++;
    if (m.isExp) expCount++;
  });

  function updateWeekBtnLabel() {
    if (state.weekMode === 1) {
      weekCycleBtn.innerHTML = '&#9200; Only Ends This Wk (' + weekCount + ')';
      weekCycleBtn.className = 'chip active';
    } else if (state.weekMode === 2) {
      weekCycleBtn.innerHTML = '&#128683; Hiding Ends This Wk (' + weekCount + ')';
      weekCycleBtn.className = 'chip warn-active';
    } else {
      weekCycleBtn.innerHTML = '&#9200; Ends This Wk (' + weekCount + ')';
      weekCycleBtn.className = 'chip';
    }
  }
  updateWeekBtnLabel();

  if (expCount > 0) {
    expBtn.hidden = false;
    expBtn.innerHTML = '&#128584; Hide Expired (' + expCount + ')';
  } else {
    expBtn.hidden = true;
    state.hideExp = false;
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
    list.sort(function (elA, elB) {
      var a = cardMetaMap[elA.getAttribute('data-ident')];
      var b = cardMetaMap[elB.getAttribute('data-ident')];
      if (a.isFlyer !== b.isFlyer) return a.isFlyer ? -1 : 1;

      var r = 0;
      if (key === 'disc') {
        r = cmpNullLast(a.disc, b.disc, -1);
      } else if (key === 'plow') {
        r = cmpNullLast(a.price, b.price, 1);
      } else if (key === 'phigh') {
        r = cmpNullLast(a.price, b.price, -1);
      } else if (key === 'end') {
        if (a.isExp !== b.isExp) r = a.isExp ? 1 : -1;
        else if (a.end && !b.end) r = -1;
        else if (!a.end && b.end) r = 1;
        else if (a.end && b.end) r = a.end < b.end ? -1 : (a.end > b.end ? 1 : 0);
      } else if (key === 'endlate') {
        if (a.end && !b.end) r = -1;
        else if (!a.end && b.end) r = 1;
        else if (a.end && b.end) r = a.end > b.end ? -1 : (a.end < b.end ? 1 : 0);
      } else if (key === 'cat') {
        r = a.cat < b.cat ? -1 : (a.cat > b.cat ? 1 : 0);
      }
      return r || (a.idx - b.idx);
    });
    var frag = document.createDocumentFragment();
    list.forEach(function (el) { frag.appendChild(el); });
    frag.appendChild(dbResults);
    frag.appendChild(emptyMsg);
    grid.appendChild(frag);
  }

  function addOrUpdateItem(id, itemData) {
    savedMap[id] = itemData;
    save(STORE_KEY, savedMap);
    if (!cardMetaMap[id]) {
      buildCarryoverCard(id, itemData);
    } else {
      syncCardControls(cardMetaMap[id].el, id);
    }
    render();
  }

  function createCustomFromQuery(targetStatus) {
    var raw = q.value.trim();
    if (!raw) return;
    var priceMatch = /\$?\s*(\d+(?:\.\d{1,2})?)\s*$/.exec(raw);
    var parsedPrice = null;
    var cleanName = raw;
    if (priceMatch && raw.replace(priceMatch[0], '').trim().length >= 2) {
      parsedPrice = parseFloat(priceMatch[1]);
      cleanName = raw.replace(priceMatch[0], '').trim();
    }
    var detectedCat = state.activeCat || (
      /\b(milk|egg|bread|chicken|beef|pork|salmon|cheese|butter|yogurt|fruit|apple|banana|berry|salad|spinach|bok\s*choy|cauliflower|rice|pasta|coffee|tea|coke|soda|water|juice|chips|snack|nuts|chia|oats|cereal|sauce|oil|honey|pizza|dumpling|tofu|bacon|sausage|steak|bagel|croissant|muffin)\b/i.test(cleanName)
        ? 'groceries'
        : 'other'
    );
    var id = 'custom:' + hashStr(cleanName.toLowerCase());
    addOrUpdateItem(id, {
      status: targetStatus,
      qty: 1,
      got: false,
      name: cleanName + (parsedPrice !== null ? ' ($' + parsedPrice.toFixed(2) + ')' : ''),
      url: '',
      sku: '',
      cat: detectedCat,
      price: parsedPrice,
      disc: null,
      end: '',
      custom: true
    });
    q.value = '';
    dbResults.hidden = true;
    render();
  }

  addCustomCartBtn.addEventListener('click', function () { createCustomFromQuery('cart'); });
  addCustomNextBtn.addEventListener('click', function () { createCustomFromQuery('next'); });
  q.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && q.value.trim()) {
      e.preventDefault();
      createCustomFromQuery(state.viewMode === 'next' ? 'next' : 'cart');
    }
  });

  function runPastCatalogSearch() {
    var term = q.value.trim().toLowerCase();
    if (term.length < 2) return;
    dbResults.hidden = false;
    dbResults.innerHTML = '<h4>Searching local catalog & price database...</h4>';

    fetchFullPriceDb(function (db) {
      var matches = [];
      Object.keys(db).forEach(function (id) {
        var entry = db[id];
        var hay = ((entry.name || '') + ' ' + (entry.item_no || '') + ' ' + (entry.cat || '')).toLowerCase();
        if (hay.indexOf(term) !== -1) {
          matches.push({ id: id, entry: entry });
        }
      });

      if (!matches.length) {
        dbResults.innerHTML = '<h4>No matches found in your database for "' + esc(term) + '". Use "+ Add to Cart" above for a custom entry!</h4>';
        return;
      }

      var html = '<h4>Catalog & Price Database Matches (' + matches.length + ')</h4>';
      matches.slice(0, 20).forEach(function (m) {
        var e = m.entry;
        var lastHist = e.history && e.history.length ? e.history[e.history.length - 1] : null;
        var lastPrice = lastHist ? lastHist.price : (e.price !== undefined ? e.price : null);
        var priceTxt = lastPrice !== null ? '$' + Number(lastPrice).toFixed(2) + (lastHist ? ' (seen ' + lastHist.date + ')' : '') : 'No price recorded';
        var imgTag = e.url ? '<img src="' + esc(e.url) + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : '';
        html += '<div class="db-row" data-dbid="' + esc(m.id) + '">' +
                  '<div style="display:flex;align-items:center;gap:.55rem;min-width:0;">' + imgTag +
                    '<div style="overflow:hidden;text-overflow:ellipsis;"><b>' + esc(e.name || m.id) + '</b><br><span style="color:var(--muted);">' + priceTxt + ' &middot; <span style="text-transform:capitalize;">' + esc(e.cat || 'other') + '</span></span></div>' +
                  '</div>' +
                  '<div style="display:flex;gap:.3rem;flex-shrink:0;">' +
                    '<button type="button" class="chip active db-add-cart" data-id="' + esc(m.id) + '">+ Cart</button>' +
                    '<button type="button" class="chip next-active db-add-next" data-id="' + esc(m.id) + '">&#128204; Next</button>' +
                  '</div>' +
                '</div>';
      });
      dbResults.innerHTML = html;

      // Bind add buttons
      Array.prototype.slice.call(dbResults.querySelectorAll('.db-add-cart, .db-add-next')).forEach(function (btn) {
        btn.addEventListener('click', function () {
          var id = btn.getAttribute('data-id');
          var target = btn.classList.contains('db-add-next') ? 'next' : 'cart';
          var e = db[id];
          if (!e) return;
          var lastH = e.history && e.history.length ? e.history[e.history.length - 1] : null;
          var pVal = lastH ? lastH.price : (e.price !== undefined ? e.price : null);
          addOrUpdateItem(id, {
            status: target, qty: 1, got: false,
            name: e.name || id, url: e.url || '', sku: e.item_no || '',
            cat: e.cat || 'other', price: pVal, disc: null, end: '', custom: false
          });
          btn.textContent = 'Added!';
        });
      });
    });
  }

  if (searchHistBtn) {
    searchHistBtn.addEventListener('click', runPastCatalogSearch);
  }

  function updateCatCounts() {
    var counts = {};
    origOrder.forEach(function (el) {
      var m = cardMetaMap[el.getAttribute('data-ident')];
      if (!m || m.isCarry) return;
      counts[m.cat] = (counts[m.cat] || 0) + 1;
    });
    catBtns.forEach(function (btn) {
      var c = btn.getAttribute('data-cat');
      var span = btn.querySelector('.cnt');
      if (span) span.textContent = counts[c] || 0;
      btn.hidden = !counts[c];
    });
    if (state.activeCat && !counts[state.activeCat]) state.activeCat = '';
  }

  catFixesCopy.addEventListener('click', function () {
    var skus = {};
    Object.keys(overrides).forEach(function (k) { if (k.indexOf('sku:') === 0) skus[k.slice(4)] = overrides[k]; });
    var text = JSON.stringify({ skus: skus, words: {} }, null, 2);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { catFixesCopy.textContent = 'Copied!'; setTimeout(function () { catFixesCopy.textContent = 'Copy as overrides'; }, 1600); });
    } else { window.prompt('Copy into category_overrides.json:', text); }
  });

  function render() {
    updateCatCounts();
    var t = q.value.trim().toLowerCase();
    quickAdd.hidden = t.length === 0;
    if (t.length > 0) {
      quickAddLabel.innerHTML = 'Custom item: <b>' + q.value.trim().replace(/</g, '&lt;') + '</b>';
    } else {
      dbResults.hidden = true;
    }

    grid.classList.toggle('no-img', state.noImg);
    setToggle(imgToggleBtn, state.noImg);
    setToggle(taxToggleBtn, state.bcTax);

    var shown = 0;
    var cartCount = 0, nextCount = 0, gotCount = 0;
    var cartSubtotal = 0, cartTax = 0, cartUnpriced = 0;
    var nextSubtotal = 0;

    Object.keys(savedMap).forEach(function (id) {
      var item = savedMap[id];
      var qty = item.qty || 1;
      var p = item.price;
      var rate = taxRate(item.name, item.cat || 'other');
      if (item.status === 'cart') {
        cartCount++;
        if (item.got) gotCount++;
        if (p === null || p === undefined) cartUnpriced++;
        else {
          cartSubtotal += p * qty;
          cartTax += p * qty * rate;
        }
      } else if (item.status === 'next') {
        nextCount++;
        if (p !== null && p !== undefined) nextSubtotal += p * qty;
      }
    });

    origOrder.forEach(function (el) {
      var id = el.getAttribute('data-ident');
      var m = cardMetaMap[id];
      var saved = savedMap[id];
      var inCart = !!(saved && saved.status === 'cart');
      var inNext = !!(saved && saved.status === 'next');

      var modeOk = true;
      if (state.viewMode === 'cart') modeOk = inCart;
      else if (state.viewMode === 'next') modeOk = inNext;
      else modeOk = !m.isCarry; // Hide carryover/custom items in main flyer browse view unless in Cart/Next view

      var ok = modeOk &&
               (!t || m.t.indexOf(t) !== -1) &&
               (!state.newOnly || m.isNew) &&
               (!state.saleOnly || m.isSale) &&
               (!state.atlOnly || m.isAtl) &&
               (!state.clrOnly || m.isClr) &&
               (state.weekMode !== 1 || m.isWeek) &&
               (state.weekMode !== 2 || !m.isWeek) &&
               (!state.hideExp || !m.isExp) &&
               (!state.hideRep || !m.isSeen) &&
               (!state.activeCat || m.cat === state.activeCat);
      el.hidden = !ok;
      if (ok) shown++;
    });

    listBtn.innerHTML = state.viewMode === 'cart' ? 'Show Flyer' : '&#128722; Cart (' + cartCount + ')';
    listBtn.classList.toggle('active', state.viewMode === 'cart');

    nextBtn.innerHTML = state.viewMode === 'next' ? 'Show Flyer' : '&#128204; Next Wk (' + nextCount + ')';
    nextBtn.classList.toggle('active', state.viewMode === 'next');

    setToggle(newBtn, state.newOnly);
    setToggle(saleBtn, state.saleOnly);
    setToggle(atlBtn, state.atlOnly);
    setToggle(clrBtn, state.clrOnly);
    updateWeekBtnLabel();
    setToggle(expBtn, state.hideExp);
    setToggle(repBtn, state.hideRep);

    var activeFilters = (state.newOnly ? 1 : 0) + (state.saleOnly ? 1 : 0) + (state.atlOnly ? 1 : 0) +
      (state.clrOnly ? 1 : 0) + (state.weekMode ? 1 : 0) + (state.hideExp ? 1 : 0) +
      (state.hideRep ? 1 : 0) + (state.activeCat ? 1 : 0);
    filtersCount.textContent = activeFilters ? ' (' + activeFilters + ')' : '';
    filtersToggle.classList.toggle('active', activeFilters > 0);

    catBtns.forEach(function (btn) {
      setToggle(btn, state.activeCat === btn.getAttribute('data-cat'));
    });

    var showBar = cartCount > 0 || nextCount > 0 || state.viewMode !== 'all';
    listBar.hidden = !showBar;
    promoteNextBtn.hidden = !(state.viewMode === 'next' && nextCount > 0);
    clearGotBtn.hidden = !(gotCount > 0);

    if (state.viewMode === 'next') {
      listSum.textContent = 'Pinned for Next Week: ' + nextCount + ' item' + (nextCount === 1 ? '' : 's') +
                            ' · $' + nextSubtotal.toFixed(2);
    } else {
      var displayTotal = state.bcTax ? (cartSubtotal + cartTax) : cartSubtotal;
      var taxNote = state.bcTax ? ' (incl. $' + cartTax.toFixed(2) + ' tax)' : '';
      listSum.textContent = 'Cart: ' + cartCount + (cartCount === 1 ? ' item' : ' items') +
                            ' · $' + displayTotal.toFixed(2) + taxNote +
                            (cartUnpriced ? ' (+' + cartUnpriced + ' unpriced)' : '');
    }

    shownEl.textContent = shown;

    if (shown === 0) {
      if (t.length >= 2 && dbResults.hidden) {
        runPastCatalogSearch();
      }
      emptyMsg.textContent = state.viewMode === 'cart'
        ? 'Your cart is empty. Tick "Cart" on any item or type a custom item above.'
        : state.viewMode === 'next'
          ? 'No items saved for next week yet. Tap "📌 Next" on any card to save it.'
          : 'No items on this post match your filter.';
    }
    emptyMsg.hidden = shown !== 0;
  }

  // Import shared links (#list=...)
  (function importShared() {
    var m = /[#&]list=([0-9a-z.]+)/.exec(location.hash || '');
    if (!m) return;
    var ids = [];
    m[1].split('.').forEach(function (h) { if (hashToIdent[h]) ids.push(hashToIdent[h]); });
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}
    if (!ids.length) return;
    if (!window.confirm('Add ' + ids.length + ' shared item' + (ids.length === 1 ? '' : 's') + ' to your Cart?')) return;
    ids.forEach(function (id) {
      var cm = cardMetaMap[id];
      if (cm) {
        savedMap[id] = extractCardData(cm.el, 'cart', 1, false);
        syncCardControls(cm.el, id);
      }
    });
    save(STORE_KEY, savedMap);
    state.viewMode = 'cart';
  })();

  function activeBucketItems() {
    var target = state.viewMode === 'next' ? 'next' : 'cart';
    return Object.keys(savedMap).filter(function (id) { return savedMap[id].status === target; }).map(function (id) {
      return { id: id, data: savedMap[id] };
    });
  }

  function shareUrl() {
    var base = location.href.split('#')[0];
    var hashes = activeBucketItems().map(function (it) { return hashStr(it.id); });
    return base + '#list=' + hashes.join('.');
  }

  function formatChecklistText() {
    var items = activeBucketItems();
    var title = state.viewMode === 'next' ? 'Costco Next Week List' : 'Costco Shopping List';
    var lines = [title + ' (' + items.length + '):'];
    var sum = 0;
    items.forEach(function (it) {
      var d = it.data;
      var qStr = (d.qty && d.qty > 1) ? (d.qty + 'x ') : '';
      var pStr = (d.price !== null && d.price !== undefined) ? (' - $' + (d.price * (d.qty || 1)).toFixed(2)) : '';
      if (d.price !== null && d.price !== undefined) sum += d.price * (d.qty || 1);
      var check = d.got ? '[x] ' : '[ ] ';
      lines.push(check + qStr + d.name + pStr);
    });
    lines.push('Total: $' + sum.toFixed(2));
    lines.push(shareUrl());
    return lines.join(NL);
  }

  copyBtn.addEventListener('click', function () {
    var items = activeBucketItems();
    if (!items.length) return;
    var text = formatChecklistText();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        var orig = copyBtn.innerHTML;
        copyBtn.innerHTML = '&#10003; Copied!';
        setTimeout(function () { copyBtn.innerHTML = orig; }, 1600);
      });
    } else {
      window.prompt('Copy your checklist:', text);
    }
  });

  shareBtn.addEventListener('click', function () {
    var items = activeBucketItems();
    if (!items.length) return;
    var url = shareUrl();
    if (navigator.share) {
      navigator.share({ title: 'Costco Shopping List', text: formatChecklistText(), url: url }).catch(function () {});
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () { window.alert('Share link copied to clipboard.'); });
    } else {
      window.prompt('Copy share link:', url);
    }
  });

  listBtn.addEventListener('click', function () {
    state.viewMode = (state.viewMode === 'cart') ? 'all' : 'cart';
    render();
  });
  nextBtn.addEventListener('click', function () {
    state.viewMode = (state.viewMode === 'next') ? 'all' : 'next';
    render();
  });
  if (newBtn) newBtn.addEventListener('click', function () { state.newOnly = !state.newOnly; render(); });
  if (atlBtn) atlBtn.addEventListener('click', function () { state.atlOnly = !state.atlOnly; render(); });
  if (repBtn) repBtn.addEventListener('click', function () { state.hideRep = !state.hideRep; render(); });
  saleBtn.addEventListener('click', function () { state.saleOnly = !state.saleOnly; render(); });
  clrBtn.addEventListener('click', function () { state.clrOnly = !state.clrOnly; render(); });

  weekCycleBtn.addEventListener('click', function () {
    state.weekMode = (state.weekMode + 1) % 3;
    render();
  });
  expBtn.addEventListener('click', function () {
    state.hideExp = !state.hideExp;
    save(HIDE_EXP_KEY, state.hideExp);
    render();
  });
  imgToggleBtn.addEventListener('click', function () {
    state.noImg = !state.noImg;
    save(NO_IMG_KEY, state.noImg);
    render();
  });
  taxToggleBtn.addEventListener('click', function () {
    state.bcTax = !state.bcTax;
    save(TAX_KEY, state.bcTax);
    render();
  });
  promoteNextBtn.addEventListener('click', function () {
    Object.keys(savedMap).forEach(function (id) {
      if (savedMap[id].status === 'next') {
        savedMap[id].status = 'cart';
        if (cardMetaMap[id]) syncCardControls(cardMetaMap[id].el, id);
      }
    });
    save(STORE_KEY, savedMap);
    state.viewMode = 'cart';
    render();
  });
  clearGotBtn.addEventListener('click', function () {
    Object.keys(savedMap).forEach(function (id) {
      if (savedMap[id].status === 'cart' && savedMap[id].got) {
        delete savedMap[id];
        if (cardMetaMap[id]) syncCardControls(cardMetaMap[id].el, id);
      }
    });
    save(STORE_KEY, savedMap);
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

  q.addEventListener('input', function () {
    dbResults.hidden = true;
    render();
    if (q.value.trim().length >= 3 && Number(shownEl.textContent) < 3) {
      runPastCatalogSearch();
    }
  });

  resetBtn.addEventListener('click', function () {
    q.value = '';
    dbResults.hidden = true;
    state.viewMode = 'all';
    state.newOnly = false;
    state.saleOnly = false;
    state.atlOnly = false;
    state.clrOnly = false;
    state.weekMode = 0;
    state.hideExp = false;
    state.hideRep = false;
    state.activeCat = '';
    state.sort = 'default';
    sortSel.value = 'default';
    save(HIDE_EXP_KEY, false);
    applySort();
    render();
  });

  clearBtn.addEventListener('click', function () {
    var target = state.viewMode === 'next' ? 'next' : 'cart';
    var label = target === 'next' ? 'Next Week list' : 'Cart';
    if (!window.confirm('Clear all items from your ' + label + '?')) return;
    Object.keys(savedMap).forEach(function (id) {
      if (savedMap[id].status === target) {
        delete savedMap[id];
        if (cardMetaMap[id]) syncCardControls(cardMetaMap[id].el, id);
      }
    });
    save(STORE_KEY, savedMap);
    state.viewMode = 'all';
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

  filtersToggle.addEventListener('click', function () {
    filtersPanel.hidden = !filtersPanel.hidden;
    filtersToggle.setAttribute('aria-expanded', filtersPanel.hidden ? 'false' : 'true');
  });

  function onScroll() {
    var y = window.pageYOffset || document.documentElement.scrollTop || 0;
    topBtn.classList.toggle('show', y > 500);
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  topBtn.addEventListener('click', function () {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  onScroll();

  updateFixesNote();
  applySort();
  render();
})();
</script>
</body>
</html>"""

CAT_ICONS = (
    ("groceries", "&#129382;", "Groceries"),
    ("household", "&#129532;", "Household"),
    ("health", "&#128138;", "Health"),
    ("home", "&#129692;", "Home"),
    ("appliances", "&#129368;", "Appliances"),
    ("electronics", "&#128187;", "Electronics"),
    ("clothing", "&#128084;", "Clothing"),
    ("other", "&#128230;", "Other"),
)


def render_html(
    items,
    monday_url,
    weekend_url,
    store_location,
    is_weekend_mode,
    exclusive_urls=None,
    seen_ids=None,
    valid_range=None,
    stale=False,
    price_db=None,
) -> str:
    stamp = datetime.now(ZoneInfo("America/Vancouver")).strftime("%b %d, %I:%M %p")
    esc = html.escape
    exclusive_urls = exclusive_urls or set()
    seen_ids = seen_ids or set()
    price_db = price_db or {}

    sale_count = sum(1 for i in items if i.disc_pct is not None)
    clr_count = sum(1 for i in items if i.clearance)
    new_count = len(exclusive_urls)
    repeat_count = sum(1 for i in items if i.ident in seen_ids)
    atl_ids = {i.ident for i in items if is_all_time_low(i, price_db)}
    atl_count = len(atl_ids)

    cat_counts = Counter(i.category for i in items)
    cat_btns = []
    for cat_key, icon, title in CAT_ICONS:
        cnt = cat_counts.get(cat_key, 0)
        hid = "" if cnt else " hidden"
        cat_btns.append(
            f'<button class="chip" data-cat="{cat_key}" type="button" aria-pressed="false" title="{title}"{hid}>{icon} <span class="cnt">{cnt}</span></button>'
        )
    cat_btns_html = "\n    ".join(cat_btns)

    if valid_range and stale:
        valid_line = (
            f"&#9203; <b>Waiting for this week's post</b> &ndash; showing the one for "
            f"{fmt_range(valid_range)} (ended)<br>"
        )
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
        new_btn_html = ""
        source_url = monday_url
        source_name = "Monday Sales Post"

    atl_btn_html = (
        f'<button class="chip" id="atl-toggle" type="button" aria-pressed="false" title="Lowest recorded price in your database">&#128293; All-Time Low ({atl_count})</button>'
        if atl_count
        else ""
    )

    repeat_btn_html = (
        f'<button class="chip" id="rep-toggle" type="button" aria-pressed="false" '
        f'title="Hide items that were also on last week\'s post">'
        f"&#128257; Hide Repeats ({repeat_count})</button>"
        if repeat_count
        else ""
    )

    embedded_hist = {}
    cards = []
    for n, i in enumerate(items):
        is_new = i.url in exclusive_urls
        is_atl = i.ident in atl_ids
        is_repeat = i.ident in seen_ids

        if i.ident in price_db and price_db[i.ident].get("history"):
            embedded_hist[i.ident] = price_db[i.ident]["history"]

        classes = ["card"]
        if is_new:
            classes.append("is-new")
        if i.clearance:
            classes.append("is-clr")
        if i.disc_pct is not None:
            classes.append("is-sale")
        if is_atl:
            classes.append("is-atl")
        class_str = " ".join(classes)

        attrs = (
            f'data-t="{esc((i.label + " " + (i.item_no or "")).lower(), quote=True)}" '
            f'data-name="{esc(i.label, quote=True)}" '
            f'data-url="{esc(i.url, quote=True)}" '
            f'data-ident="{esc(i.ident, quote=True)}" '
            f'data-sku="{esc(i.item_no or "")}" '
            f'data-cat="{esc(i.category, quote=True)}" '
            f'data-idx="{n}"'
        )
        if i.is_flyer:
            attrs += ' data-flyer="1"'
        if i.end_date:
            attrs += f' data-end="{i.end_date}"'
        if i.price is not None:
            attrs += f' data-price="{i.price:.2f}"'
        if i.disc_pct:
            attrs += f' data-disc="{i.disc_pct}"'
        if is_repeat:
            attrs += ' data-seen="1"'

        new_tag = '<div class="new-tag">&#10024; New</div>' if is_new else ""
        disc_tag = f'<div class="disc-tag">-{i.disc_pct}%</div>' if i.disc_pct else ""
        atl_tag = '<div class="atl-tag">&#128293; Low</div>' if is_atl else ""
        clr_tag = (
            '<div class="clr-tag">&#127991;&#65039; .97</div>' if i.clearance else ""
        )
        right_tags = (
            f'<div class="tags-right">{disc_tag}{atl_tag}{clr_tag}</div>'
            if (i.disc_pct or is_atl or i.clearance)
            else ""
        )
        end_badge = '<span class="end-badge" hidden></span>' if i.end_date else ""
        repeat_tag = (
            '<span class="repeat-tag" title="Also on last week\'s post">&#128257;</span>'
            if is_repeat
            else ""
        )
        sku_badge = (
            f'<span class="sku-badge" title="View SKU history">#{esc(i.item_no)}</span>'
            if i.item_no
            else ""
        )
        foot_right = f'<span class="foot-right">{sku_badge}{repeat_tag}{end_badge}</span>'

        cards.append(
            f'''<figure class="{class_str}" {attrs}>
  {new_tag}{right_tags}
  <a href="{esc(i.url, quote=True)}" target="_blank" rel="noopener">
    <img src="{esc(i.url, quote=True)}" alt="{esc(i.label, quote=True)}" loading="lazy" referrerpolicy="no-referrer">
  </a>
  <figcaption title="Click to view SKU price history">{esc(i.label)}</figcaption>
  <div class="card-footer">
    <div class="card-actions">
      <label class="shop-label">
        <input type="checkbox" class="shop-check" data-ident="{esc(i.ident, quote=True)}"> Cart
      </label>
      <button type="button" class="mini-btn next-item-btn" data-ident="{esc(i.ident, quote=True)}" title="Save for next week">&#128204; Next</button>
      <button type="button" class="mini-btn qty-btn" data-ident="{esc(i.ident, quote=True)}" title="Tap to change quantity" hidden>&times;1</button>
      <button type="button" class="mini-btn got-btn" data-ident="{esc(i.ident, quote=True)}" title="Mark as in your physical cart" hidden>&#10003;</button>
    </div>
    {foot_right}
  </div>
</figure>'''
        )

    cards_html = "\n".join(cards) or '<p class="empty">No items found.</p>'

    return (
        PAGE_TEMPLATE.replace("__PAGE_TITLE__", page_title)
        .replace("__TOTAL__", str(len(items)))
        .replace("__SALE_COUNT__", str(sale_count))
        .replace("__CLR_COUNT__", str(clr_count))
        .replace("__NEW_BTN__", new_btn_html)
        .replace("__ATL_BTN__", atl_btn_html)
        .replace("__REPEAT_BTN__", repeat_btn_html)
        .replace("__CAT_BTNS__", cat_btns_html)
        .replace("__LOCATION__", esc(store_location or "Metro Vancouver Area"))
        .replace("__VALID_LINE__", valid_line)
        .replace("__STAMP__", stamp)
        .replace("__SOURCE_URL__", esc(source_url, quote=True))
        .replace("__SOURCE_NAME__", source_name)
        .replace("__EMBEDDED_HIST__", json.dumps(embedded_hist, separators=(",", ":")))
        .replace("__CARDS__", cards_html)
    )


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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
    monday_soup = BeautifulSoup(monday_html, "html.parser")
    monday_range = reconcile_range(
        ranges.get("monday") or range_from_soup(monday_soup),
        published_date_from_soup(monday_soup),
    )
    monday_items = extract_items_from_soup(monday_soup, monday_url)

    if not monday_items:
        print(
            "ERROR: zero images extracted from base sales post; refusing to deploy.",
            file=sys.stderr,
        )
        return 1

    store_location = extract_location_from_soup(monday_soup, monday_html)

    weekend_range = ranges.get("weekend")
    is_weekend_mode = bool(weekend_url)
    weekend_items = []
    exclusive = []
    exclusive_urls = set()

    if is_weekend_mode:
        print(f"Weekend Update: {weekend_url}")
        time.sleep(random.uniform(1, 3))
        weekend_html = fetch(scraper, weekend_url)
        weekend_soup = BeautifulSoup(weekend_html, "html.parser")
        weekend_range = reconcile_range(
            weekend_range or range_from_soup(weekend_soup),
            published_date_from_soup(weekend_soup),
        )
        if weekend_over(weekend_range, weekend_forced, today, monday_range):
            print(
                f"Weekend post ended {weekend_range[1]}; using the Monday post instead."
            )
            weekend_items = []
        else:
            weekend_items = extract_items_from_soup(weekend_soup, weekend_url)

        if weekend_items:
            store_location = (
                extract_location_from_soup(weekend_soup, weekend_html) or store_location
            )
            exclusive = find_exclusive(weekend_items, monday_items)
            exclusive_urls = {i.url for i in exclusive}
        else:
            is_weekend_mode = False

    target_items = weekend_items if is_weekend_mode else monday_items
    target_url = weekend_url if is_weekend_mode else monday_url
    mode = "weekend" if is_weekend_mode else "flyer"
    valid_range = weekend_range if is_weekend_mode else monday_range
    stale = (not is_weekend_mode) and bool(monday_range) and monday_range[1] < today

    prev_state, state_ok = load_previous_state(scraper)

    apply_categories(target_items, prev_state.get("price_db") or {}, load_category_overrides())

    seen_ids = previous_ids(prev_state, mode, target_url)
    ids = sorted({i.ident for i in target_items})
    history = build_history(prev_state, mode, target_url, ids)
    price_db = update_price_db(prev_state, target_items, valid_range)

    html_content = render_html(
        target_items,
        monday_url,
        weekend_url or monday_url,
        store_location,
        is_weekend_mode,
        exclusive_urls,
        seen_ids,
        valid_range,
        stale,
        price_db,
    )

    (OUTPUT_DIR / "index.html").write_text(html_content, encoding="utf-8")

    (OUTPUT_DIR / "state.json").write_text(
        json.dumps(
            {
                "weekend_url": weekend_url if is_weekend_mode else None,
                "flyer_url": monday_url,
                "mode": mode,
                "valid": [valid_range[0].isoformat(), valid_range[1].isoformat()]
                if valid_range
                else None,
                "total_count": len(target_items),
                "exclusive_count": len(exclusive),
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "history": history,
                "price_db": price_db,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    maybe_notify(prev_state, state_ok, target_url, is_weekend_mode, target_items, exclusive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
