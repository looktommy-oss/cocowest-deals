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
    (re.compile(r"\b(sound\s*bar|apple\s*watch|apple\s*tv|apple\s*airpods?|apple\s*ipad|apple\s*macbook|apple\s*pencil|surge\s*protector|power\s*bank|dash\s*cam|memory\s*card|sd\s*card|gift\s*card)\b", re.I), "electronics"),
    (re.compile(r"\b(water\s*flosser|waterpik|body\s*butter|body\s*wash|hand\s*soap|hand\s*cream|face\s*cream|eye\s*cream|shave\s*cream|shaving\s*cream|lip\s*balm|tea\s*tree|epsom\s*salt|milk\s*thistle|protein\s*powder|whey\s*protein|collagen\s*peptides?|electrolyte\s*powder|liquid\s*i\.?v\.?|contact\s*lens|heating\s*pad)\b", re.I), "health"),
    (re.compile(r"\b(motor\s*oil|synthetic\s*oil|windshield\s*wash|wiper\s*blade|cat\s*litter|dog\s*food|cat\s*food|dog\s*treats?|cat\s*treats?|pee\s*pads?|dish\s*soap|laundry\s*detergent|dishwasher\s*tabs?|dishwasher\s*pods?|dryer\s*sheets?|garbage\s*bags?|trash\s*bags?|food\s*wrap|parchment\s*paper|aluminum\s*foil|furnace\s*filter)\b", re.I), "household"),
    (re.compile(r"\b(bar\s*stool|towel\s*bar|towel\s*warmer|tea\s*towel|bath\s*towel|bath\s*mat|welcome\s*mat|area\s*rug|bed\s*frame|picture\s*frame|sleeping\s*bag|duffel\s*bag|garment\s*bag|luggage|step\s*stool|folding\s*table|folding\s*chair|office\s*chair|gaming\s*chair|patio\s*umbrella|fire\s*pit|water\s*bottle|travel\s*mug)\b", re.I), "home"),
    (re.compile(r"\b(water\s*pitcher|brita|air\s*purifier|water\s*cooler|ice\s*maker|coffee\s*maker|espresso\s*machine|rice\s*cooker|slow\s*cooker|pressure\s*cooker|air\s*fryer|stand\s*mixer|hand\s*mixer|immersion\s*blender|food\s*processor|food\s*saver|vacuum\s*sealer|bread\s*maker|waffle\s*maker)\b", re.I), "appliances"),
)

CATEGORY_RULES = {
    "groceries": re.compile(
        r"\b(coffee|espresso\s*beans|k-?cup|nescafe|starbucks|tim\s*hortons|tea|matcha|chai|kombucha|"
        r"snack|meat|chicken|beef|pork|salmon|shrimp|prawn|halibut|cod|tilapia|trout|tuna|lobster|crab|scallop|mussel|calamari|sushi|"
        r"bacon|sausage|wiener|hot\s*dog|pepperoni|salami|prosciutto|pancetta|chorizo|ham|turkey|deli|roast|steak|striploin|ribeye|sirloin|tenderloin|brisket|ribs|wings|meatball|burger|kebab|souvlaki|gyro|"
        r"juice|lemonade|smoothie|milk|oat\s*milk|almond\s*milk|coconut\s*milk|cream\s*cheese|sour\s*cream|whipping\s*cream|creamer|half\s*&\s*half|butter|ghee|margarine|egg|yogurt|kefir|"
        r"cheese|cheddar|mozzarella|parmesan|brie|gouda|feta|havarti|provolone|swiss|halloumi|paneer|queso|babybel|laughing\s*cow|boursin|"
        r"chips|doritos|lays|ruffles|pringles|cheetos|tostitos|miss\s*vickie|popcorn|pretzel|cracker|ritz|triscuit|wheat\s*thin|goldfish|rice\s*cake|jerky|biltong|pork\s*rind|"
        r"chocolate|lindt|ferrero|ghirardelli|godiva|kit\s*kat|reeses|snickers|mars|twix|m&m|gummy|candy|licorice|caramel|toffee|fudge|marshmallow|mint|gum|"
        r"cookie|oreo|biscotti|shortbread|brownie|macaron|muffin|croissant|danish|scone|bagel|brioche|bread|loaf|sourdough|ciabatta|baguette|flatbread|naan|pita|tortilla|wrap|english\s*muffin|pancake|waffle|crepe|pie|cake|cheesecake|tiramisu|strudel|strudels|tart|donut|churro|"
        r"cereal|cheerios|granola|oats|oatmeal|quaker|porridge|muesli|granola\s*bar|protein\s*bar|clif|kind\s*bar|rxbar|nature\s*valley|nutri-?grain|"
        r"almond|cashew|pistachio|walnut|pecan|macadamia|hazelnut|peanut|mixed\s*nuts|trail\s*mix|pumpkin\s*seed|sunflower|chia|flax|hemp|raisin|cranberry|date|fig|prune|apricot|"
        r"rice|jasmine|basmati|quinoa|couscous|barley|lentil|chickpea|beans?|pasta|spaghetti|penne|macaroni|fettuccine|lasagna|ravioli|tortellini|gnocchi|noodle|ramen|udon|pho|vermicelli|"
        r"sauce|marinara|pesto|alfredo|salsa|guacamole|hummus|tzatziki|dip|ketchup|mustard|mayo|mayonnaise|aioli|relish|bbq|teriyaki|soy\s*sauce|tamari|sriracha|hot\s*sauce|vinegar|balsamic|olive\s*oil|avocado\s*oil|canola\s*oil|coconut\s*oil|sesame\s*oil|cooking\s*spray|dressing|marinade|"
        r"soup|broth|stock|bouillon|chili|stew|curry|chowder|"
        r"flour|sugar|sweetener|stevia|erythritol|honey|maple\s*syrup|agave|jam|jelly|preserve|peanut\s*butter|almond\s*butter|nutella|tahini|cocoa|baking|yeast|vanilla|spice|seasoning|garlic\s*powder|cinnamon|peppercorn|sea\s*salt|himalayan\s*salt|"
        r"fruit|apple|banana|orange|mandarin|clementine|grapefruit|lemon|lime|berry|strawberry|blueberry|raspberry|blackberry|grape|cherry|peach|pear|plum|mango|pineapple|melon|watermelon|kiwi|avocado|tomato|"
        r"vegetable|salad|lettuce|spinach|kale|arugula|coleslaw|broccoli|cauliflower|asparagus|brussels|celery|carrot|cucumber|zucchini|pepper|mushroom|onion|garlic|potato|sweet\s*potato|fries|hash\s*brown|tater|corn|peas|edamame|tofu|tempeh|kimchi|sauerkraut|pickle|olive|caper|"
        r"pizza|dumpling|gyoza|wonton|potsticker|spring\s*roll|egg\s*roll|samosa|spanakopita|quiche|burrito|taquito|empanada|perogy|pierogi|poutine|"
        r"ice\s*cream|gelato|sorbet|popsicle|haagen|magnum|drumstick|frozen\s*yogurt|"
        r"soda|coke|coca-?cola|pepsi|sprite|7up|ginger\s*ale|root\s*beer|dr\s*pepper|canada\s*dry|bubly|perrier|san\s*pellegrino|sparkling\s*water|spring\s*water|coconut\s*water|red\s*bull|monster|celsius|alani|gatorade|powerade|prime|torani|wine|beer|spirits|liquor|vodka|whisky|gin|rum|tequila|prosecco|champagne)s?\b",
        re.I,
    ),
    "appliances": re.compile(
        r"\b(toaster|blender|vitamix|nutribullet|ninja|espresso|keurig|nespresso|breville|delonghi|cuisinart|kitchenaid|"
        r"air\s*fryer|microwave|mixer|cookware|pan|skillet|wok|frying\s*pan|saucepan|stockpot|dutch\s*oven|cast\s*iron|griddle|baking\s*sheet|bakeware|henckels|zwilling|tramontina|all-?clad|t-?fal|circulon|meyer|pyrex|corningware|knives|knife\s*set|knife\s*block|"
        r"kettle|tiger|zojirushi|vacuum|dyson|shark|roomba|irobot|roborock|tineco|bissell|hoover|shop-?vac|carpet\s*cleaner|steam\s*mop|"
        r"shaver|braun|norelco|philips\s*shaver|trimmer|wahl|toothbrush|sonicare|oral-?b\s*io|"
        r"fan|woozo|vornado|lasko|heater|air\s*conditioner|portable\s*ac|dehumidifier|humidifier|air\s*purifier|winix|levoit|blueair|"
        r"grill|weber|traeger|blackstone|masterbuilt|smoker|cooker|instant\s*pot|crock-?pot|sous\s*vide|"
        r"fridge|refrigerator|freezer|beverage\s*center|wine\s*cooler|danby|hisense|midea|frigidaire|whirlpool|"
        r"iron|garment\s*steamer|conair|hair\s*dryer|blow\s*dryer|straightener|curling\s*iron|revlon|sodastream|ice\s*maker|juicer|food\s*processor|panini)\b",
        re.I,
    ),
    "electronics": re.compile(
        r"\b(tv|television|oled|qled|uhd|4k|8k|mini-?led|monitor|display|ultrawide|"
        r"speaker|headphone|earbud|earphone|soundbar|subwoofer|receiver|amplifier|turntable|"
        r"laptop|notebook|chromebook|macbook|imac|mac\s*mini|desktop|pc|all-?in-?one|tablet|ipad|surface|kindle|e-?reader|"
        r"phone|smartphone|iphone|galaxy|pixel|motorola|unlocked|"
        r"charger|magsafe|cable|lightning|usb-?c|thunderbolt|hdmi|adapter|dock|docking\s*station|hub|"
        r"router|mesh\s*wi-?fi|modem|ethernet|tp-?link|deco|netgear|orbi|eero|asus\s*wifi|"
        r"camera|webcam|dslr|mirrorless|canon|nikon|gopro|insta360|dji|drone|dash\s*cam|security\s*camera|ring|blink|arlo|reolink|lorex|doorbell|"
        r"smart\s*watch|garmin|fitbit|apple\s*watch|galaxy\s*watch|airtag|tracker|"
        r"projector|airpods?|samsung|sony|bose|jbl|sonos|klipsch|shokz|jabra|beats|skullcandy|sennheiser|"
        r"battery|batteries|duracell|energizer|ups|cyberpower|apc|surge|power\s*bank|anker|belkin|"
        r"ssd|hard\s*drive|nvme|flash\s*drive|thumb\s*drive|microsd|sandisk|seagate|western\s*digital|lexar|"
        r"fire\s*stick|roku|chromecast|apple\s*tv|nintendo|playstation|ps5|xbox|meta\s*quest|joy-?con|controller|gaming\s*headset|gaming\s*mouse|mechanical\s*keyboard|razer|logitech|corsair|hyperx|printer|epson|brother|hp\s*envy|hp\s*laserjet|ink\s*cartridge|toner)\b",
        re.I,
    ),
    "household": re.compile(
        r"\b(paper\s*towel|bounty|sponge\s*towel|toilet\s*paper|bath\s*tissue|charmin|cashmere|royale|scott|facial\s*tissue|kleenex|scotties|napkins?|"
        r"detergent|tide|gain|persil|purex|woolite|downy|bounce|fabric\s*softener|scent\s*booster|unstopables|oxiclean|resolve|stain\s*remover|bleach|clorox|"
        r"dish\s*soap|dawn|palmolive|cascade|finish|jet-?dry|rinse\s*aid|dishwasher|"
        r"trash\s*bag|garbage\s*bag|glad|hefty|compost\s*bag|foil|alcan|reynolds|ziploc|freezer\s*bag|sandwich\s*bag|parchment|cling\s*wrap|saran|press'n\s*seal|"
        r"sponge|scotch-?brite|scrub\s*daddy|microfiber|swiffer|vileda|o-?cedar|mop|broom|dustpan|duster|lint\s*roller|"
        r"cleaner|disinfectant|lysol|mr\.\s*clean|pine-?sol|windex|pledge|febreze|air\s*wick|glade|vim|comet|barkeepers|clr|drain\s*cleaner|drano|toilet\s*bowl|"
        r"storage\s*container|food\s*storage|snapware|rubbermaid|sistema|cambro|tupperware|mason\s*jar|"
        r"light\s*bulb|led\s*bulb|feit|filtrete|furnace\s*filter|air\s*filter|brita\s*filter|command\s*strip|duct\s*tape|scotch\s*tape|packing\s*tape|wd-?40|"
        r"cat\s*litter|clumping\s*litter|dog\s*food|cat\s*food|pet\s*food|dog\s*treat|greenies|bully\s*stick|pee\s*pad|dog\s*bed|"
        r"motor\s*oil|mobil\s*1|castrol|pennzoil|windshield|wiper\s*blade|michelin|ice\s*melt|pest\s*control|raid|ant\s*trap|mouse\s*trap|propane|charcoal)\b",
        re.I,
    ),
    "clothing": re.compile(
        r"\b(mens?|womens?|ladies|kids?|boys?|girls?|youth|toddler|infant|unisex|apparel|"
        r"pants?|jeans|denim|chinos?|khakis?|trousers?|slacks|leggings?|tights|joggers?|sweatpants?|track\s*pants|capris?|shorts?|skirt|skort|dress|"
        r"hoodie|sweatshirt|pullover|fleece|sweater|cardigan|vest|jacket|coat|parka|puffer|windbreaker|raincoat|trench|blazer|flannel|"
        r"shirt|t-?shirt|tee|polo|henley|blouse|tunic|tank\s*top|camisole|base\s*layer|thermal|"
        r"underwear|boxers?|briefs?|trunks|bra|bralette|panties|bikini|socks?|ankle\s*socks|crew\s*socks|merino\s*socks|"
        r"pyjamas?|pajamas?|pjs?|loungewear|robe|bathrobe|sleepshirt|nightgown|swimsuit|swimwear|swim\s*trunks|rashguard|"
        r"shoes?|sneakers?|runners?|boots?|rain\s*boots|winter\s*boots|slippers?|sandals?|slides?|loafers?|clogs?|crocs|birkenstock|sorel|hunter|dearfoams|"
        r"gloves?|mittens?|hat|toque|beanie|cap|scarf|belt|sunglasses|"
        r"puma|adidas|nike|under\s*armour|reebok|champion|fila|new\s*balance|skechers|asics|columbia|north\s*face|helly\s*hansen|eddie\s*bauer|gerry|spyder|orvis|"
        r"calvin\s*klein|ck\b|tommy\s*hilfiger|levi'?s|wrangler|lee\b|nautica|lacoste|banana\s*republic|gap|haggar|izod|roots|bench|buffalo|dkny|mondetta|kyodan|tuff\s*athletics|kirkland\s*signature\s*(mens?|womens?|ladies|logo|crewneck|jeans))\b",
        re.I,
    ),
    "health": re.compile(
        r"\b(vitamin|multivitamin|supplement|softgels?|tablets?|capsules?|caplets?|lozenges?|chewables?|"
        r"magnesium|omega-?3|fish\s*oil|krill\s*oil|coq10|probiotic|prebiotic|collagen|biotin|melatonin|glucosamine|chondroitin|turmeric|curcumin|ashwagandha|elderberry|echinacea|ginseng|ginkgo|lutein|zinc|iron|calcium|potassium|b12|d3|vitamin\s*[a-z]|centrum|jamieson|webber\s*naturals|nature'?s\s*bounty|organika|youtheory|smartypants|l'il\s*critters|"
        r"protein\s*powder|whey|isolate|creatine|bcaa|pre-?workout|electrolyte|liquid\s*i\.?v|biosteel|vega|orgain|premier\s*protein|fairlife|ensure|boost|glucerna|"
        r"pain\s*relief|advil|tylenol|motrin|aleve|aspirin|ibuprofen|acetaminophen|naproxen|voltaren|robax|salonpas|muscle\s*relaxant|"
        r"allergy|reactine|claritin|zyrtec|aerius|allegra|benadryl|flonase|fluticasone|nasal\s*spray|sinus|cold\s*&\s*flu|dayquil|nyquil|buckley|mucinex|robitussin|halls|strepsils|vicks|emergen-?c|"
        r"metamucil|psyllium|benefiber|restoralax|laxative|senokot|imodium|pepto|tums|gaviscon|zantac|pepcid|nexium|omeprazole|gravol|digestive|lactaid|lactase|"
        r"shampoo|conditioner|pantene|head\s*&\s*shoulders|dove|tresemme|herbal\s*essences|nexxus|ogx|moroccanoil|minoxidil|rogaine|"
        r"body\s*wash|bar\s*soap|ivory\s*body|olay|old\s*spice|irish\s*spring|aveeno|cerave|cetaphil|neutrogena|eucerin|lubriderm|vaseline|aquaphor|gold\s*bond|jergens|nivea|roche-?posay|bioderma|biore|"
        r"lotion|moisturizer|cleanser|face\s*wash|toner|micellar|serum|retinol|hyaluronic|niacinamide|eye\s*cream|night\s*cream|acne|pimple|hydrocolloid|"
        r"sunscreen|sunblock|spf\s*\d+|coppertone|banana\s*boat|ombrelle|lip\s*balm|chapstick|eos|burt'?s\s*bees|"
        r"toothpaste|colgate|crest|sensodyne|pronamel|mouthwash|listerine|therabreath|floss|glide|plackers|interdental|denture|polident|"
        r"deodorant|antiperspirant|degree|secret|mitchum|native|schmidt|razor|gillette|venus|schick|bic\s*soleil|shave\s*gel|edge\s*shave|"
        r"eye\s*drops?|systane|refresh|visine|contact\s*solution|opti-?free|biotrue|re-?nu|"
        r"bandage|band-?aid|polysporin|first\s*aid|gauze|rubbing\s*alcohol|hydrogen\s*peroxide|epsom|heating\s*pad|blood\s*pressure|thermometer|oximeter|cpap|incontinence|depends|poise|always|tampax|kotex|clearblue)\b",
        re.I,
    ),
    "home": re.compile(
        r"\b(pillow|sheet|bed\s*sheet|flannel\s*sheet|microfiber\s*sheet|thread\s*count|pillowcase|blanket|throw|heated\s*throw|duvet|comforter|quilt|coverlet|mattress|sealy|serta|novaform|tempur|beautyrest|mattress\s*pad|mattress\s*topper|mattress\s*protector|memory\s*foam|"
        r"towel|bath\s*towel|bath\s*sheet|hand\s*towel|washcloth|bath\s*mat|shower\s*curtain|shower\s*head|moen|kohler|delta|water\s*ridge|faucet|bidet|brondell|toto|"
        r"rug|area\s*rug|runner|doormat|anti-?fatigue\s*mat|curtain|drape|blackout|blind|shade|"
        r"hanger|velvet\s*hanger|storage\s*bin|tote|basket|hamper|laundry\s*basket|organizer|closet|shoe\s*rack|drying\s*rack|ironing\s*board|shelving|utility\s*rack|gorilla\s*rack|safe|sentrysafe|"
        r"lamp|floor\s*lamp|table\s*lamp|desk\s*lamp|string\s*light|led\s*strip|chandelier|night\s*light|flashlight|lantern|headlamp|"
        r"candle|yankee\s*candle|diffuser|reed\s*diffuser|essential\s*oil|mirror|clock|frame|wall\s*art|"
        r"desk|standing\s*desk|chair|office\s*chair|dining\s*chair|recliner|ottoman|bar\s*stool|sofa|couch|sectional|loveseat|futon|nightstand|bookcase|bookshelf|cabinet|console|furniture|lifetime\s*table|cosco\s*step|ladder|little\s*giant|"
        r"patio|gazebo|pergola|sunjoy|yardistry|umbrella|adirondack|fire\s*pit|planter|flower\s*pot|potting\s*soil|fertilizer|grass\s*seed|hose|nozzle|sprinkler|pressure\s*washer|snow\s*shovel|snow\s*blower|generator|firman|lawn\s*mower|trimmer|leaf\s*blower|greenworks|dewalt|milwaukee|makita|craftsman|tool\s*set|socket\s*set|wrench|screwdriver|drill|"
        r"luggage|suitcase|carry-?on|spinner|samsonite|delsey|swissgear|backpack|duffel|cooler|igloo|coleman|yeti|titan\s*cooler|stanley|thermos|hydro\s*flask|contigo|owala|tumbler|water\s*bottle|tent|sleeping\s*bag|camping\s*chair|air\s*mattress|kayak|paddle\s*board|golf|callaway|taylor\s*made|kirkland\s*signature\s*golf|pickleball|dumbbell|yoga\s*mat|foam\s*roller)\b",
        re.I,
    ),
}

HEALTH_UNIT_RE = re.compile(
    r"\b\d+\s*(softgels?|tablets?|capsules?|caplets?|lozenges?|gummies|mcg|iu|spf)\b",
    re.I,
)
ELEC_UNIT_RE = re.compile(
    r"\b(\d{2,3}\s*\"|\d+\s*(tb|gb|mhz|ghz|watts?|lumens?)|1080p|1440p|2160p|wi-?fi\s*[67])\b",
    re.I,
)


def detect_category(label: str) -> str:
    if not label:
        return "other"
    for rx, cat in CATEGORY_OVERRIDES:
        if rx.search(label):
            return cat

    scores = Counter()
    for cat, pattern in CATEGORY_RULES.items():
        hits = pattern.findall(label)
        if hits:
            scores[cat] += len(hits)

    if HEALTH_UNIT_RE.search(label):
        scores["health"] += 2
    if ELEC_UNIT_RE.search(label):
        scores["electronics"] += 2

    if not scores:
        return "other"
    return scores.most_common(1)[0][0]


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

  function showHistoryModal(ident, name, sku) {
    modalTitle.textContent = name;
    modal.hidden = false;
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

  function render() {
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
        if cnt > 0:
            cat_btns.append(
                f'<button class="chip" data-cat="{cat_key}" type="button" aria-pressed="false" title="{title}">{icon} {cnt}</button>'
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
