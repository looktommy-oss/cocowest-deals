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
    return monday,
