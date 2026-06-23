#!/usr/bin/env python3
"""
artwall -- Museum Art Wallpaper for Linux
==========================================
Fetches public domain artwork from major museum APIs, composites a metadata
card (title, artist, year) onto the image, and sets it as your Linux desktop
wallpaper on a configurable schedule.

Supported museum sources
  Metropolitan Museum of Art   https://metmuseum.org    (no API key required)
  Art Institute of Chicago     https://artic.edu        (no API key required)
  Rijksmuseum Amsterdam        https://rijksmuseum.nl   (free API key required)

Supported filters (configured in config.ini)
  categories   paintings, drawings, prints, sculpture, photographs, textiles
  regions      Dutch, Flemish, Italian, French, Spanish, Japanese, ...
  themes       portrait, landscape, mythology, religious, still life, ...
  media        oil on canvas, watercolor, tempera, engraving, ...
  date_min     earliest year (negative = BCE)
  date_max     latest year

Usage
  artwall.py                    fetch and set a new wallpaper
  artwall.py --dry-run          fetch and composite but do not set wallpaper
  artwall.py --verbose          show detailed debug output
  artwall.py --info             show metadata about the current wallpaper
  artwall.py --source met       force a specific museum source
  artwall.py --config PATH      use a non-default config file

Configuration
  Copy config.ini.example to ~/.config/artwall/config.ini and edit to taste.

Project  https://github.com/YOUR_USERNAME/artwall
License  MIT
"""

VERSION = "1.0.0"

# Identify artwall to museum APIs and CDNs. Some providers (AIC) return 403
# for requests with the default Python user-agent.
USER_AGENT = "artwall/1.0.0 (https://github.com/caseychambliss/artwall)"

import argparse
import configparser
import json
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── ANSI colour helpers (Rule 13) ─────────────────────────────────────────────
# green=OK/success  yellow=warnings  red=errors  cyan=info steps

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"


def clr(code: str, s: str) -> str:
    return f"{code}{s}{RESET}"


def step(n: int, total: int, msg: str):
    """Print a numbered progress step (Rule 14)."""
    print(f"  {clr(CYAN, f'[{n}/{total}]')} {msg}")


def ok(msg: str):
    print(f"  {clr(GREEN, 'OK')}   {msg}")


def warn(msg: str):
    print(f"  {clr(YELLOW, 'WARN')} {msg}")


def err(msg: str):
    print(f"  {clr(RED, 'ERR')}  {msg}", file=sys.stderr)


def dbg(msg: str, verbose: bool = False):
    if verbose:
        print(f"  {clr(CYAN, '..')}   {msg}")


TOTAL_STEPS = 5  # used in step() calls throughout main()


# ── Dependency preflight (Rule 15) ────────────────────────────────────────────
# Check all required packages before doing any other work.

def preflight():
    missing = []
    try:
        import requests  # noqa: F401
    except ImportError:
        missing.append(("requests", "pip install requests"))
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append(("Pillow", "pip install Pillow"))
    if missing:
        err("Missing dependencies:")
        for pkg, cmd in missing:
            print(f"      {pkg:12s}  {clr(YELLOW, cmd)}")
        print()
        sys.exit(1)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Artwork:
    """
    Normalised artwork record returned by any museum source.

    All string fields may be empty if the museum record does not include that
    information. Consumers should handle empty strings gracefully.
    """
    title:       str  # artwork title
    artist:      str  # artist display name
    year:        str  # human-readable date string, e.g. "ca. 1665" or "1503-1519"
    medium:      str  # materials and technique, e.g. "Oil on canvas"
    culture:     str  # culture or place of origin
    image_url:   str  # direct URL to a high-resolution JPEG or PNG
    source_name: str  # human-readable museum name
    artwork_url: str  # URL to the museum's web page for this work
    fact:        str = ""  # optional 1-2 sentence fact about the artwork or artist

    def label_lines(self, show_source: bool = True, show_facts: bool = False) -> list[str]:
        """Flat list of strings for the overlay card. See also label_data()."""
        return [text for text, _ in self.label_data(show_source=show_source, show_facts=show_facts)]

    def label_data(
        self, show_source: bool = True, show_facts: bool = False
    ) -> list[tuple[str, str]]:
        """
        Return ordered (text, role) pairs for the overlay card.

        Roles and their rendering:
          title   -- large bold
          artist  -- medium regular
          detail  -- smaller regular  (year | medium)
          source  -- smaller regular
          fact    -- smaller italic   (wrapped by compositor to fit card width)

        Line order:
          1. Title (always present; falls back to "Untitled")
          2. Artist (omitted if unknown)
          3. Year  |  Medium  (omitted if both unknown; medium truncated at 80 chars)
          4. Source name (optional, controlled by show_source)
          5. Fact text (optional, controlled by show_facts; wrapped by compositor)
        """
        data: list[tuple[str, str]] = [(self.title or "Untitled", "title")]
        if self.artist:
            data.append((self.artist, "artist"))
        medium_short = (self.medium[:80] + "…") if len(self.medium) > 80 else self.medium
        detail = "  |  ".join(p for p in (self.year, medium_short) if p)
        if detail:
            data.append((detail, "detail"))
        if show_source and self.source_name:
            data.append((self.source_name, "source"))
        if show_facts and self.fact:
            data.append((self.fact, "fact"))
        return data


@dataclass
class Filters:
    """
    User-specified filter criteria, loaded from the [filters] section of
    config.ini.

    All lists are lowercase-normalised at load time. Empty list means "no
    filter for this dimension" (i.e. accept anything).
    """
    categories: list[str] = field(default_factory=list)
    regions:    list[str] = field(default_factory=list)
    themes:     list[str] = field(default_factory=list)
    media:      list[str] = field(default_factory=list)
    date_min:   Optional[int] = None
    date_max:   Optional[int] = None

    def keyword_string(self) -> str:
        """
        Flatten regions + themes + media into a single space-separated string
        suitable for use as a museum API keyword search query.
        Excludes categories, which are mapped to structured API parameters
        separately by each source class.
        """
        return " ".join(self.regions + self.themes + self.media)

    def in_date_range(self, year_begin: Optional[int], year_end: Optional[int]) -> bool:
        """
        Return True if the artwork's date range overlaps the configured range.
        Works with partial information -- if year_begin or year_end is None,
        that bound is not checked.
        """
        if self.date_min is not None and year_end is not None:
            if year_end < self.date_min:
                return False
        if self.date_max is not None and year_begin is not None:
            if year_begin > self.date_max:
                return False
        return True


# ── Configuration loader ──────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "artwall" / "config.ini"


def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not path.exists():
        err(f"Config file not found: {path}")
        print(f"  Copy config.ini.example to {path} and edit to taste.")
        sys.exit(1)
    cfg.read(path)
    return cfg


def parse_csv(cfg: configparser.ConfigParser, section: str, key: str) -> list[str]:
    """Read a comma-separated config value into a lowercase-stripped list."""
    raw = cfg.get(section, key, fallback="")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def load_filters(cfg: configparser.ConfigParser) -> Filters:
    return Filters(
        categories = parse_csv(cfg, "filters", "categories"),
        regions    = parse_csv(cfg, "filters", "regions"),
        themes     = parse_csv(cfg, "filters", "themes"),
        media      = parse_csv(cfg, "filters", "media"),
        date_min   = cfg.getint("filters", "date_min", fallback=None),
        date_max   = cfg.getint("filters", "date_max", fallback=None),
    )


# ── Source: Metropolitan Museum of Art ───────────────────────────────────────
# API docs: https://metmuseum.github.io/
# No API key required. Rate limit: 80 requests per second (generous).

# Department IDs used to narrow search by category.
# See https://collectionapi.metmuseum.org/public/collection/v1/departments
MET_DEPARTMENT_IDS = {
    "paintings":   11,   # European Paintings
    "drawings":     8,   # Drawings and Prints (also covers prints)
    "prints":       8,   # Drawings and Prints
    "sculpture":   12,   # Greek and Roman Art (largest sculpture collection)
    "photographs": 19,   # Photographs
}


class MetSource:
    NAME      = "Metropolitan Museum of Art"
    BASE      = "https://collectionapi.metmuseum.org/public/collection/v1"
    MAX_TRIES = 12  # number of random object IDs to try before giving up

    def fetch_random(self, filters: Filters, verbose: bool = False) -> Optional[Artwork]:
        """
        Search the Met collection with active filters, pick a random result,
        and return a populated Artwork record with a usable image URL.

        Strategy:
          1. Build a keyword query from filters.keyword_string().
             Falls back to "painting" if no keywords are active.
          2. If exactly one category maps to a single Met department, pass
             departmentId to narrow results without keyword noise.
          3. Shuffle the returned objectID list and try them one by one
             until one has a public-domain image and passes date filters.
        """
        import requests

        kw = filters.keyword_string() or "painting"
        params: dict = {
            "isPublicDomain": "true",
            "hasImages":      "true",
            "q":              kw,
        }

        dept_ids = list({
            MET_DEPARTMENT_IDS[c]
            for c in filters.categories
            if c in MET_DEPARTMENT_IDS
        })
        if len(dept_ids) == 1:
            params["departmentId"] = dept_ids[0]
            dbg(f"Met: narrowing to department {dept_ids[0]}", verbose)

        dbg(f"Met search params: {params}", verbose)

        try:
            r = requests.get(f"{self.BASE}/search", params=params, timeout=10,
                            headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            warn(f"Met search request failed: {exc}")
            return None

        object_ids: list[int] = data.get("objectIDs") or []
        if not object_ids:
            warn("Met: no results for current filters")
            return None

        dbg(f"Met: {len(object_ids)} candidates", verbose)
        random.shuffle(object_ids)

        for oid in object_ids[:self.MAX_TRIES]:
            artwork = self._fetch_object(oid, filters, verbose)
            if artwork:
                return artwork

        warn("Met: exhausted retries without finding a usable image")
        return None

    def _fetch_object(
        self, object_id: int, filters: Filters, verbose: bool
    ) -> Optional[Artwork]:
        import requests
        try:
            r = requests.get(f"{self.BASE}/objects/{object_id}", timeout=10,
                            headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            obj = r.json()
        except Exception as exc:
            dbg(f"Met object {object_id} fetch error: {exc}", verbose)
            return None

        image_url = obj.get("primaryImage", "")
        if not image_url:
            dbg(f"Met object {object_id}: no image", verbose)
            return None

        year_begin = obj.get("objectDateBegin")
        year_end   = obj.get("objectDateEnd")
        if not filters.in_date_range(year_begin, year_end):
            dbg(f"Met object {object_id}: outside date range", verbose)
            return None

        return Artwork(
            title       = obj.get("title", "Untitled"),
            artist      = obj.get("artistDisplayName", ""),
            year        = obj.get("objectDate", ""),
            medium      = obj.get("medium", ""),
            culture     = obj.get("culture", ""),
            image_url   = image_url,
            source_name = self.NAME,
            artwork_url = obj.get("objectURL", ""),
            fact        = self._build_fact(obj),
        )

    @staticmethod
    def _build_fact(obj: dict) -> str:
        """
        Synthesise a brief fact from Met API fields that are present in every
        object response without extra API calls.

        Combines period/dynasty information with thematic tag terms.
        Examples: "Renaissance period. Themes: Portrait, Landscape, Garden."
                  "Qing dynasty. Themes: Birds, Flowers."
        """
        parts = []
        period  = obj.get("period", "").strip()
        dynasty = obj.get("dynasty", "").strip()
        if period:
            parts.append(f"{period} period.")
        elif dynasty:
            parts.append(f"{dynasty} dynasty.")
        tags = [
            t.get("term", "").strip()
            for t in (obj.get("tags") or [])
            if t.get("term")
        ]
        if tags:
            parts.append(f"Themes: {', '.join(tags[:4])}.")
        return " ".join(parts)


# ── Source: Art Institute of Chicago ─────────────────────────────────────────
# API docs: https://api.artic.edu/docs/
# No API key required. Elasticsearch-backed, excellent structured filtering.

AIC_CLASSIFICATION_MAP = {
    "paintings":   "Painting",
    "drawings":    "Drawing and Watercolor",
    "prints":      "Print",
    "sculpture":   "Sculpture",
    "photographs": "Photograph",
    "textiles":    "Textile",
}

AIC_FIELDS = (
    "id,title,artist_display,date_display,date_start,date_end,"
    "medium_display,image_id,place_of_origin,classification_title,"
    "description,is_public_domain"
)


class AICSource:
    NAME      = "Art Institute of Chicago"
    BASE      = "https://api.artic.edu/api/v1"
    MAX_PAGE  = 50   # maximum page index to request (random page selection)
    MAX_TRIES = 10

    def fetch_random(self, filters: Filters, verbose: bool = False) -> Optional[Artwork]:
        """
        Request a random page of AIC results with the active filters applied.
        AIC supports structured Elasticsearch parameters for classification and
        place of origin alongside free-text keyword search.
        """
        import requests

        page = random.randint(1, self.MAX_PAGE)
        params: dict = {
            "fields":                             AIC_FIELDS,
            "limit":                              20,
            "page":                               page,
            "query[term][is_public_domain]":      "true",
        }

        # Category filter: map to AIC's classification_title field
        aic_classes = [
            AIC_CLASSIFICATION_MAP[c]
            for c in filters.categories
            if c in AIC_CLASSIFICATION_MAP
        ]
        if len(aic_classes) == 1:
            params["query[term][classification_title.keyword]"] = aic_classes[0]
            dbg(f"AIC: filtering by classification '{aic_classes[0]}'", verbose)

        # Region filter: map to AIC's place_of_origin field
        # Only apply as a structured filter if exactly one region is active;
        # multiple regions require a boolean OR query not supported here.
        if len(filters.regions) == 1:
            params["query[term][place_of_origin.keyword]"] = filters.regions[0].title()
            dbg(f"AIC: filtering by place_of_origin '{filters.regions[0].title()}'", verbose)
        elif filters.regions:
            params["q"] = " ".join(filters.regions + filters.themes + filters.media)
        elif filters.themes or filters.media:
            params["q"] = " ".join(filters.themes + filters.media)

        dbg(f"AIC params: {params}", verbose)

        try:
            r = requests.get(f"{self.BASE}/artworks", params=params, timeout=10,
                            headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            warn(f"AIC request failed: {exc}")
            return None

        items: list[dict] = data.get("data", [])
        if not items:
            warn(f"AIC: no results on page {page} for current filters")
            return None

        dbg(f"AIC: {len(items)} results on page {page}", verbose)
        random.shuffle(items)

        for item in items[:self.MAX_TRIES]:
            artwork = self._parse_item(item, filters, verbose)
            if artwork:
                return artwork

        warn("AIC: exhausted retries without finding a usable image")
        return None

    def _parse_item(
        self, item: dict, filters: Filters, verbose: bool
    ) -> Optional[Artwork]:
        image_id = item.get("image_id")
        if not image_id:
            dbg(f"AIC item {item.get('id')}: no image_id", verbose)
            return None

        if not filters.in_date_range(item.get("date_start"), item.get("date_end")):
            dbg(f"AIC item {item.get('id')}: outside date range", verbose)
            return None

        # AIC IIIF image URL: request 1200px wide for high quality
        image_url   = f"https://www.artic.edu/iiif/2/{image_id}/full/1200,/0/default.jpg"
        artwork_url = f"https://www.artic.edu/artworks/{item.get('id', '')}"

        return Artwork(
            title       = item.get("title", "Untitled"),
            artist      = (item.get("artist_display", "") or "").split("\n")[0].strip(),
            year        = item.get("date_display", ""),
            medium      = item.get("medium_display", ""),
            culture     = item.get("place_of_origin", ""),
            image_url   = image_url,
            source_name = self.NAME,
            artwork_url = artwork_url,
            fact        = self._extract_description(item.get("description", "") or ""),
        )

    @staticmethod
    def _extract_description(raw: str) -> str:
        """
        Extract a clean first sentence from AIC's description field.

        AIC descriptions are HTML strings. This strips tags and returns the
        first sentence (up to 200 characters), trimmed of whitespace.
        """
        if not raw:
            return ""
        # Strip HTML tags
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        # Take first sentence
        sentences = re.split(r"(?<=[.!?])\s+", text)
        first = sentences[0].strip() if sentences else ""
        # Hard cap at 200 characters to keep the overlay readable
        return first[:200] + ("…" if len(first) > 200 else "")


# ── Source: Rijksmuseum Amsterdam ─────────────────────────────────────────────
# API docs: https://data.rijksmuseum.nl/object-metadata/api/
# Free API key required: https://www.rijksmuseum.nl/en/research/conduct-research/
#   data/access-to-and-use-of-the-rijksmuseum-api

# Dutch type names used by the Rijksmuseum API
RK_TYPE_MAP = {
    "paintings":   "schilderij",
    "drawings":    "tekening",
    "prints":      "prent",
    "sculpture":   "beeldhouwwerk",
    "photographs": "foto",
    "textiles":    "textiel",
}


class RijksmuseumSource:
    NAME      = "Rijksmuseum Amsterdam"
    BASE      = "https://www.rijksmuseum.nl/api/en/collection"
    MAX_PAGE  = 30
    MAX_TRIES = 10

    def __init__(self, api_key: str):
        self.api_key = api_key

    def fetch_random(self, filters: Filters, verbose: bool = False) -> Optional[Artwork]:
        """
        Search the Rijksmuseum collection. The API supports structured filters
        for type (object category), material, date range, and free-text search.
        """
        import requests

        page = random.randint(0, self.MAX_PAGE)
        params: dict = {
            "key":     self.api_key,
            "ps":      20,         # page size
            "p":       page,
            "imgonly": "True",     # only objects with images
            "format":  "json",
        }

        # Category -> Dutch type name
        for cat in filters.categories:
            if cat in RK_TYPE_MAP:
                params["type"] = RK_TYPE_MAP[cat]
                dbg(f"Rijksmuseum: filtering by type '{params['type']}'", verbose)
                break

        # Keyword search from regions + themes
        kw = " ".join(filters.regions + filters.themes)
        if kw:
            params["q"] = kw

        # Material (first media item -- API accepts one material at a time)
        if filters.media:
            params["material"] = filters.media[0]

        # Date range
        if filters.date_min is not None:
            params["yearfrom"] = filters.date_min
        if filters.date_max is not None:
            params["yearto"] = filters.date_max

        dbg(f"Rijksmuseum params: {params}", verbose)

        try:
            r = requests.get(self.BASE, params=params, timeout=10,
                            headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            warn(f"Rijksmuseum request failed: {exc}")
            return None

        items: list[dict] = data.get("artObjects", [])
        if not items:
            warn(f"Rijksmuseum: no results on page {page} for current filters")
            return None

        dbg(f"Rijksmuseum: {len(items)} results on page {page}", verbose)
        random.shuffle(items)

        for item in items[:self.MAX_TRIES]:
            artwork = self._parse_item(item, verbose)
            if artwork:
                return artwork

        warn("Rijksmuseum: exhausted retries without finding a usable image")
        return None

    def _parse_item(self, item: dict, verbose: bool) -> Optional[Artwork]:
        img = item.get("webImage", {}) or {}
        image_url = img.get("url", "")
        if not image_url:
            dbg(f"Rijksmuseum item {item.get('objectNumber')}: no image URL", verbose)
            return None

        # Request a larger version where supported (=s0 = original size)
        image_url = re.sub(r"=s\d+$", "=s1200", image_url)

        # Extract a year from the long title string using a four-digit year pattern
        long_title = item.get("longTitle", "")
        year_match = re.search(r"\b(1[0-9]{3}|2[0-9]{3})\b", long_title)
        year = year_match.group(1) if year_match else ""

        return Artwork(
            title       = item.get("title", "Untitled"),
            artist      = item.get("principalOrFirstMaker", ""),
            year        = year,
            medium      = "",   # not available in collection list endpoint
            culture     = "Dutch",
            image_url   = image_url,
            source_name = self.NAME,
            artwork_url = (item.get("links") or {}).get("web", ""),
            fact        = self._extract_long_title_fact(long_title, year),
        )

    @staticmethod
    def _extract_long_title_fact(long_title: str, year: str) -> str:
        """
        Derive a brief context sentence from the Rijksmuseum longTitle field.

        The longTitle typically reads like:
          "Girl with a Pearl Earring, Johannes Vermeer, c. 1665"
        or
          "The Night Watch, Rembrandt van Rijn, 1642, oil on canvas, 379.5 x 453.5cm"

        When richer plaque description text is needed, a future enhancement
        should call the Rijksmuseum object detail endpoint
        (GET /api/en/collection/{objectNumber}) and read
        artObject.plaqueDescriptionEnglish.
        """
        if not long_title:
            return ""
        # Strip the year we already extracted to avoid redundancy,
        # then trim to a readable length.
        fact = long_title.replace(f", {year}", "").strip().rstrip(",")
        return fact[:200] + ("…" if len(fact) > 200 else "")


# ── Image download ────────────────────────────────────────────────────────────

def download_image(url: str, dest: Path, verbose: bool = False) -> bool:
    """
    Stream-download an image from url to dest.
    Returns True on success, False on any network or HTTP error.
    """
    import requests
    headers = {"User-Agent": USER_AGENT}
    # AIC's IIIF image server requires a Referer header from the artic.edu domain
    # to serve images; without it, some images return 403.
    if "artic.edu" in url:
        headers["Referer"] = "https://www.artic.edu/"
    try:
        dbg(f"Downloading: {url}", verbose)
        r = requests.get(url, stream=True, timeout=30,
                         headers=headers)
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=8192):
                fh.write(chunk)
        size_kb = dest.stat().st_size // 1024
        dbg(f"Downloaded {size_kb} KB to {dest.name}", verbose)
        return True
    except Exception as exc:
        warn(f"Image download failed: {exc}")
        return False


# ── Overlay compositor ────────────────────────────────────────────────────────

# Maps position names (as used in config.ini) to (horizontal, vertical) alignment.
OVERLAY_POSITIONS = {
    "bottom-left":   ("left",   "bottom"),
    "bottom-center": ("center", "bottom"),
    "bottom-right":  ("right",  "bottom"),
    "top-left":      ("left",   "top"),
    "top-center":    ("center", "top"),
    "top-right":     ("right",  "top"),
}


def load_font_set(base_size: int) -> dict:
    """
    Load a set of font variants for the overlay card roles.

    Returns a dict keyed by role name:
      title   -- Bold, base_size
      artist  -- Regular, 82% of base_size
      detail  -- Regular, 68% of base_size
      source  -- Regular, 68% of base_size
      fact    -- Oblique/Italic, 68% of base_size

    Falls back to Pillow's built-in bitmap font if no system fonts are found.
    """
    from PIL import ImageFont

    BOLD_PATHS = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ]
    REGULAR_PATHS = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    OBLIQUE_PATHS = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-RI.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf",
    ]

    def load_first(paths, size):
        for p in paths:
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    continue
        return ImageFont.load_default()

    title_size  = base_size
    artist_size = max(int(base_size * 0.82), 12)
    detail_size = max(int(base_size * 0.68), 11)

    return {
        "title":  load_first(BOLD_PATHS,    title_size),
        "artist": load_first(REGULAR_PATHS, artist_size),
        "detail": load_first(REGULAR_PATHS, detail_size),
        "source": load_first(REGULAR_PATHS, detail_size),
        "fact":   load_first(OBLIQUE_PATHS, detail_size),
    }


def _wrap_to_pixel_width(
    text: str, font, max_px: int, draw
) -> list[str]:
    """
    Wrap text to fit within max_px pixels using actual font metrics.

    Estimates average character width from the full string, then uses
    textwrap with an adjusted character limit. Applies a 0.92 safety
    margin to account for variable-width character distributions.
    """
    if not text:
        return [""]
    bbox = draw.textbbox((0, 0), text, font=font)
    text_px = max(bbox[2] - bbox[0], 1)
    if text_px <= max_px:
        return [text]
    avg_char_w = text_px / max(len(text), 1)
    max_chars  = max(15, int(max_px / avg_char_w * 0.92))
    return textwrap.wrap(text, width=max_chars) or [text]


def get_screen_resolution() -> Optional[tuple]:
    """
    Detect the current display resolution by querying xrandr or xdpyinfo.

    Returns (width, height) as a tuple of ints, or None if detection fails
    (e.g. on Wayland without XWayland, or in a headless/systemd environment).
    """
    # xrandr reports: 'Screen 0: ... current 1366 x 768, ...'
    try:
        r = subprocess.run(
            ["xrandr", "--current"], capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            m = re.search(r"current (\d+) x (\d+)", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    except Exception:
        pass

    # xdpyinfo reports: '  dimensions:    1366x768 pixels'
    try:
        r = subprocess.run(
            ["xdpyinfo"], capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            m = re.search(r"dimensions:\s+(\d+)x(\d+)", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    except Exception:
        pass

    return None


def calculate_display_scale(
    img_w: int, img_h: int,
    screen_w: int, screen_h: int,
    scaling_mode: str = "scaled",
) -> float:
    """
    Calculate the scale factor at which the desktop environment will render
    the image on the physical screen.

    Used to convert a desired on-screen font size to the correct in-image
    pixel size, so text appears at a consistent apparent size regardless of
    the image's native resolution.

    scaling_mode values match the gsettings picture-options values:
      scaled    -- fit whole image, maintain aspect ratio (most common for art)
      zoom      -- crop to fill, maintain aspect ratio
      centered  -- native pixel size, no scaling
      stretched -- fill screen, ignoring aspect ratio
      wallpaper -- tile at native size
    """
    if scaling_mode == "scaled":
        return min(screen_w / img_w, screen_h / img_h)
    elif scaling_mode == "zoom":
        return max(screen_w / img_w, screen_h / img_h)
    elif scaling_mode in ("centered", "wallpaper"):
        return 1.0
    else:
        # stretched: use average of x and y scale as approximation
        return (screen_w / img_w + screen_h / img_h) / 2.0


def composite_overlay(
    image_path:  Path,
    output_path: Path,
    artwork:     Artwork,
    position:    str,
    font_size:   int,
    bg_opacity:  float,
    padding:     int,
    text_color:  tuple,
    show_source: bool,
    show_facts:  bool = False,
    scaling:     str  = "scaled",
    verbose:     bool = False,
) -> bool:
    """
    Open the image at image_path, composite a semi-transparent metadata card
    containing the artwork's label lines, and save the result as a JPEG at
    output_path.

    font_size is the desired apparent size on the user's screen in points.
    composite_overlay uses get_screen_resolution() to detect the display and
    calculate_display_scale() to determine how much the desktop environment
    will scale the image, then composites the font at the correct in-image
    size so text appears at font_size on screen regardless of the painting's
    native resolution. Falls back to a short-dimension heuristic if screen
    resolution cannot be detected.

    Returns True on success, False if the image cannot be opened or saved.
    """
    from PIL import Image, ImageDraw

    try:
        img = Image.open(image_path).convert("RGBA")
    except Exception as exc:
        err(f"Cannot open image for compositing: {exc}")
        return False

    label_pairs = artwork.label_data(show_source=show_source, show_facts=show_facts)
    if not label_pairs:
        shutil.copy(image_path, output_path)
        return True

    img_w, img_h = img.size
    # Card content must not exceed 44% of image width so it never overflows.
    max_card_content_w = min(int(img_w * 0.44), 900) - padding * 2

    # Compute in-image font size from desired on-screen size.
    # font_size = apparent size wanted on screen. We divide by the display
    # scale factor so the compositor writes pixels that appear at font_size
    # after Cinnamon/GNOME scales the image to fill the screen.
    screen_res = get_screen_resolution()
    if screen_res:
        screen_w, screen_h = screen_res
        display_scale = calculate_display_scale(
            img_w, img_h, screen_w, screen_h, scaling
        )
        font_size_in_image = max(12, int(font_size / display_scale))
        dbg(
            f"Screen {screen_w}x{screen_h}, image {img_w}x{img_h}, "
            f"scale {display_scale:.3f}x -- "
            f"{font_size}pt on screen = {font_size_in_image}pt in image",
            verbose,
        )
    else:
        # No screen detected (Wayland / headless / systemd timer context).
        # Fall back to 12% of short dimension as a reasonable heuristic.
        font_size_in_image = max(font_size, int(min(img_w, img_h) * 0.12))
        dbg(f"Screen resolution unavailable -- using {font_size_in_image}pt in image",
            verbose)

    font_set    = load_font_set(font_size_in_image)
    dummy_draw  = ImageDraw.Draw(img)

    # Expand label_pairs into (text, role, font) triples, wrapping long lines
    # to fit within the card budget using pixel-accurate font metrics.
    triples: list[tuple[str, str]] = []  # (text, font)
    for text, role in label_pairs:
        font = font_set.get(role, font_set["detail"])
        wrapped = _wrap_to_pixel_width(text, font, max_card_content_w, dummy_draw)
        for line in wrapped:
            triples.append((line, font))

    # Measure the final line set
    bboxes      = [dummy_draw.textbbox((0, 0), t, font=f) for t, f in triples]
    line_heights = [b[3] - b[1] for b in bboxes]
    line_widths  = [b[2] - b[0] for b in bboxes]
    line_spacing = int(max(line_heights, default=font_size) * 0.35)

    text_w = max(line_widths, default=0)
    text_h = sum(line_heights) + line_spacing * (len(triples) - 1)
    card_w = text_w + padding * 2
    card_h = text_h + padding * 2

    # Resolve position
    pos_key = position.lower() if position.lower() in OVERLAY_POSITIONS else "bottom-left"
    halign, valign = OVERLAY_POSITIONS[pos_key]
    img_w, img_h = img.size
    margin = padding * 2

    card_x = (
        margin if halign == "left"
        else img_w - card_w - margin if halign == "right"
        else (img_w - card_w) // 2
    )
    card_y = margin if valign == "top" else img_h - card_h - margin

    # Draw the card on a transparent overlay layer
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)
    alpha   = int(bg_opacity * 255)

    # rounded_rectangle was added in Pillow 8.2; fall back to rectangle on
    # older installations.
    try:
        draw.rounded_rectangle(
            [card_x, card_y, card_x + card_w, card_y + card_h],
            radius=10,
            fill=(0, 0, 0, alpha),
        )
    except AttributeError:
        draw.rectangle(
            [card_x, card_y, card_x + card_w, card_y + card_h],
            fill=(0, 0, 0, alpha),
        )

    # Draw text lines inside the card
    y = card_y + padding
    for (line, font), lh in zip(triples, line_heights):
        draw.text((card_x + padding, y), line, font=font, fill=(*text_color, 255))
        y += lh + line_spacing

    # Composite and save as high-quality JPEG
    result = Image.alpha_composite(img, overlay).convert("RGB")
    try:
        result.save(output_path, "JPEG", quality=95)
        dbg(f"Composited image saved: {output_path}", verbose)
        return True
    except Exception as exc:
        err(f"Failed to save composited image: {exc}")
        return False


# ── Desktop environment detection and wallpaper setter ───────────────────────

def detect_de() -> str:
    """
    Return a lowercase identifier for the current desktop environment.
    Checks DESKTOP_SESSION and XDG_CURRENT_DESKTOP environment variables.
    Returns "unknown" if neither variable contains a recognised value.
    """
    de   = os.environ.get("DESKTOP_SESSION", "").lower()
    xdg  = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    combined = de + " " + xdg
    for name in ("cinnamon", "gnome", "xfce", "kde", "mate", "budgie", "lxde"):
        if name in combined:
            return name
    return "unknown"


def set_wallpaper(image_path: Path, scaling: str = "scaled", verbose: bool = False) -> bool:
    """
    Set the desktop wallpaper to image_path.

    Supports Cinnamon, GNOME, MATE, XFCE, KDE Plasma, and makes a best-effort
    attempt for unknown environments via gsettings.

    scaling controls how the image fills the screen:
      scaled     -- fit whole image, letterboxed if needed (recommended for art)
      zoom       -- crop to fill screen (may cut off overlay card in corners)
      stretched  -- distort to fill screen
      centered   -- center at native size, no scaling
      wallpaper  -- tile

    Returns True if the wallpaper was set successfully.
    """
    uri      = image_path.as_uri()
    path_str = str(image_path)
    de       = detect_de()
    dbg(f"Desktop environment: {de}", verbose)

    # Command sequences per desktop environment.
    # Each item is a command list passed to subprocess.run().
    command_map = {
        "cinnamon": [
            ["gsettings", "set", "org.cinnamon.desktop.background",
             "picture-uri", uri],
            ["gsettings", "set", "org.cinnamon.desktop.background",
             "picture-options", scaling],
        ],
        "gnome": [
            ["gsettings", "set", "org.gnome.desktop.background",
             "picture-uri", uri],
            ["gsettings", "set", "org.gnome.desktop.background",
             "picture-uri-dark", uri],
            ["gsettings", "set", "org.gnome.desktop.background",
             "picture-options", scaling],
        ],
        "mate": [
            ["gsettings", "set", "org.mate.background",
             "picture-filename", path_str],
            ["gsettings", "set", "org.mate.background",
             "picture-options", scaling],
        ],
        "budgie": [
            ["gsettings", "set", "org.gnome.desktop.background",
             "picture-uri", uri],
            ["gsettings", "set", "org.gnome.desktop.background",
             "picture-options", scaling],
        ],
        "xfce": [
            ["xfconf-query", "-c", "xfce4-desktop",
             "-p", "/backdrop/screen0/monitor0/workspace0/last-image",
             "-s", path_str],
        ],
        "kde": [
            ["plasma-apply-wallpaperimage", path_str],
        ],
    }

    cmds = command_map.get(de, command_map["gnome"])  # fall back to GNOME

    for cmd in cmds:
        dbg(f"Running: {' '.join(cmd)}", verbose)
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode != 0:
                warn(f"Command returned non-zero: {' '.join(cmd)}")
                if verbose:
                    dbg(result.stderr.decode(errors="replace"), verbose)
                return False
        except FileNotFoundError:
            warn(f"Command not found: {cmd[0]}")
            return False
        except Exception as exc:
            warn(f"Wallpaper command failed: {exc}")
            return False

    return True


# ── Cache management ──────────────────────────────────────────────────────────

def trim_cache(cache_dir: Path, max_items: int, verbose: bool = False):
    """
    Remove the oldest cached raw images if the cache exceeds max_items files.
    Only removes raw_*.jpg files (not the composited current.jpg or metadata).
    """
    images = sorted(
        cache_dir.glob("raw_*.jpg"),
        key=lambda p: p.stat().st_mtime,
    )
    while len(images) > max_items:
        oldest = images.pop(0)
        oldest.unlink(missing_ok=True)
        dbg(f"Cache trim: removed {oldest.name}", verbose)


# ── Info display ──────────────────────────────────────────────────────────────

def show_info(cache_dir: Path):
    """Display metadata about the most recently fetched artwork."""
    meta_path = cache_dir / "current.json"
    if not meta_path.exists():
        print("  No wallpaper metadata found. Run artwall.py first.")
        return
    with open(meta_path) as fh:
        meta = json.load(fh)
    ts = meta.get("timestamp")
    fetched_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "unknown"
    print()
    print(f"  {'Title:':<10} {meta.get('title', 'Unknown')}")
    print(f"  {'Artist:':<10} {meta.get('artist', 'Unknown')}")
    print(f"  {'Year:':<10} {meta.get('year', 'Unknown')}")
    print(f"  {'Medium:':<10} {meta.get('medium', 'Unknown')}")
    print(f"  {'Culture:':<10} {meta.get('culture', 'Unknown')}")
    print(f"  {'Source:':<10} {meta.get('source_name', 'Unknown')}")
    print(f"  {'URL:':<10} {meta.get('artwork_url', 'Unknown')}")
    print(f"  {'Fetched:':<10} {fetched_str}")
    if meta.get("fact"):
        print(f"  {'Fact:':<10} {meta['fact']}")
    print()


# ── Interactive configuration editor ────────────────────────────────────────
#
# Valid option lists for settings that have a fixed set of choices.
# TODO (v2 GUI): each of these becomes a dropdown or checkbox group
# in the future graphical settings interface.

VALID_CATEGORIES = ["paintings", "drawings", "prints", "sculpture", "photographs", "textiles"]

VALID_REGIONS = [
    "Dutch", "Flemish", "Italian", "French", "Spanish", "German",
    "Japanese", "Chinese", "British", "American", "Venetian", "Roman",
    "Persian", "Mughal", "Ottoman", "Byzantine",
]

VALID_THEMES = [
    "portrait", "landscape", "still life", "mythology", "religious",
    "biblical", "allegory", "genre", "history", "battle", "marine",
    "cityscape", "flower", "nude",
]

VALID_MEDIA = [
    "oil on canvas", "oil on panel", "watercolor", "tempera", "fresco",
    "engraving", "etching", "lithograph", "gouache", "pastel",
]

VALID_POSITIONS = [
    "bottom-left", "bottom-center", "bottom-right",
    "top-left", "top-center", "top-right",
]

# TODO (v2 GUI): scaling becomes a dropdown
VALID_SCALING = ["scaled", "zoom", "stretched", "centered", "wallpaper"]


def _cfg_get(cfg: configparser.ConfigParser, section: str, key: str, fallback: str = "") -> str:
    try:
        return cfg.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return fallback


def _cfg_set(cfg: configparser.ConfigParser, section: str, key: str, value: str):
    if not cfg.has_section(section):
        cfg.add_section(section)
    cfg.set(section, key, str(value))


def _save_config(cfg: configparser.ConfigParser, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        cfg.write(fh)


def configure(config_path: Path):
    """
    Interactive configuration editor launched by --configure.

    Displays all current settings grouped by section. The user selects a
    setting to edit and is shown the appropriate input type:
      - bool settings     -> yes/no confirm
      - fixed-choice      -> arrow-key select or checkbox
      - free-text lists   -> checkbox from known values
      - numbers           -> text input with validation

    Changes are written to config_path immediately after each edit.
    """
    try:
        import questionary
    except ImportError:
        err("questionary is required for --configure")
        print(f"  Install: {clr(YELLOW, 'pip install questionary --break-system-packages')}")
        sys.exit(1)

    cfg = configparser.ConfigParser()
    if config_path.exists():
        cfg.read(config_path)
    else:
        warn(f"No config found at {config_path} -- starting with defaults")

    def get(section, key, fallback=""):
        return _cfg_get(cfg, section, key, fallback)

    def save(section, key, value, label):
        _cfg_set(cfg, section, key, value)
        _save_config(cfg, config_path)
        ok(f"Saved: {label} = {value}")

    def bool_label(section, key, fallback="true"):
        return "enabled" if get(section, key, fallback) == "true" else "disabled"

    print()
    print(f"  {clr(CYAN, 'artwall')} configuration")
    print(f"  {config_path}")
    print(f"  {clr(CYAN, 'tip:')} arrow keys to navigate, enter to select")
    print(f"        in sub-menus: ctrl-c or leave blank to go back without changing")
    print()

    last_name = None  # cursor memory: name of the last edited setting
    while True:
        # Rebuild default choice to restore cursor to last edited setting.
        # Splits on 2+ spaces to separate setting name from its value column.
        default_choice = None
        if last_name:
            for c in choices if 'choices' in dir() else []:
                if isinstance(c, str):
                    parts = re.split(r'\s{2,}', c.strip())
                    if parts and parts[0] == last_name:
                        default_choice = c
                        break
        rk_key_status = (
            "set"
            if get("sources", "rijksmuseum_api_key", "YOUR_KEY_HERE") not in ("", "YOUR_KEY_HERE")
            else "not set"
        )
        choices = [
            questionary.Separator("  General"),
            f"  Rotation interval          {get('general', 'interval_hours', '24')}h",
            f"  Cache max images           {get('general', 'cache_max', '100')}",
            questionary.Separator("  Sources"),
            f"  Met Museum                 {bool_label('sources', 'met_museum', 'true')}",
            f"  Art Institute of Chicago   {bool_label('sources', 'art_institute_chicago', 'true')}",
            f"  Rijksmuseum                {bool_label('sources', 'rijksmuseum', 'false')}",
            f"  Rijksmuseum API key        {rk_key_status}",
            f"  Source weights (Met/AIC/RK) {get('sources','met_weight','1')}/{get('sources','aic_weight','1')}/{get('sources','rijksmuseum_weight','1')}",
            questionary.Separator("  Filters"),
            f"  Categories                 {get('filters', 'categories', 'paintings') or '(all)'}",
            f"  Regions                    {get('filters', 'regions', '') or '(all)'}",
            f"  Themes                     {get('filters', 'themes', '') or '(all)'}",
            f"  Media                      {get('filters', 'media', '') or '(all)'}",
            f"  Date range                 {get('filters', 'date_min', '1400')} to {get('filters', 'date_max', '1900')}",
            questionary.Separator("  Overlay"),
            f"  Overlay enabled            {bool_label('overlay', 'enabled', 'true')}",
            f"  Position                   {get('overlay', 'position', 'bottom-left')}",
            f"  Font size                  {get('overlay', 'font_size', '28')}pt",
            f"  Background opacity         {get('overlay', 'background_opacity', '0.65')}",
            f"  Scaling                    {get('overlay', 'scaling', 'scaled')}",
            f"  Show museum name           {bool_label('overlay', 'show_source', 'true')}",
            f"  Show artwork facts          {bool_label('overlay', 'show_facts', 'false')}",
            questionary.Separator(""),
            "  Done",
        ]

        # Restore cursor to last edited setting
        default_choice = None
        if last_name:
            for c in choices:
                if isinstance(c, str):
                    parts = re.split(r'\s{2,}', c.strip())
                    if parts and parts[0] == last_name:
                        default_choice = c
                        break

        action = questionary.select(
            "Select a setting to edit  (enter=confirm, ctrl-c=quit):",
            choices=choices,
            default=default_choice,
        ).ask()

        if action is None or action.strip() == "Done":
            print()
            ok(f"Configuration saved to {config_path}")
            break

        a = action.strip()
        # Store setting name (text before the value column) for cursor memory
        name_parts = re.split(r'\s{2,}', a)
        last_name = name_parts[0] if name_parts else None
        print()

        if a.startswith("Rotation interval"):
            v = questionary.text(
                "Rotation interval in hours:",
                default=get("general", "interval_hours", "24"),
                validate=lambda v: v.isdigit() or "Must be a whole number",
            ).ask()
            if v:
                save("general", "interval_hours", v, "interval_hours")
                warn("Re-run install.sh to update the systemd timer with the new interval.")

        elif a.startswith("Cache max"):
            v = questionary.text(
                "Maximum cached images:",
                default=get("general", "cache_max", "100"),
                validate=lambda v: v.isdigit() or "Must be a whole number",
            ).ask()
            if v:
                save("general", "cache_max", v, "cache_max")

        elif a.startswith("Met Museum"):
            v = questionary.confirm(
                "Enable Met Museum?",
                default=get("sources", "met_museum", "true") == "true",
            ).ask()
            if v is not None:
                save("sources", "met_museum", str(v).lower(), "met_museum")

        elif a.startswith("Art Institute"):
            v = questionary.confirm(
                "Enable Art Institute of Chicago?",
                default=get("sources", "art_institute_chicago", "true") == "true",
            ).ask()
            if v is not None:
                save("sources", "art_institute_chicago", str(v).lower(), "art_institute_chicago")

        elif a.startswith("Rijksmuseum ") and "key" not in a.lower():
            v = questionary.confirm(
                "Enable Rijksmuseum?",
                default=get("sources", "rijksmuseum", "false") == "true",
            ).ask()
            if v is not None:
                save("sources", "rijksmuseum", str(v).lower(), "rijksmuseum")

        elif "API key" in a:
            current = get("sources", "rijksmuseum_api_key", "")
            if current == "YOUR_KEY_HERE":
                current = ""
            v = questionary.text(
                "Rijksmuseum API key:\n"
                "  Register free at https://www.rijksmuseum.nl/en/research/conduct-research/"
                "data/access-to-and-use-of-the-rijksmuseum-api\n"
                "  (leave blank to keep current):",
                default=current,
            ).ask()
            if v and v.strip():
                save("sources", "rijksmuseum_api_key", v.strip(), "rijksmuseum_api_key")

        elif a.startswith("Source weights"):
            for label, key in [("Met weight", "met_weight"), ("AIC weight", "aic_weight"), ("Rijksmuseum weight", "rijksmuseum_weight")]:
                v = questionary.text(
                    f"{label} (higher = more likely to be chosen):",
                    default=get("sources", key, "1"),
                    validate=lambda v: v.isdigit() or "Must be a whole number",
                ).ask()
                if v:
                    save("sources", key, v, key)

        elif a.startswith("Categories"):
            current = [c.strip() for c in get("filters", "categories", "").split(",") if c.strip()]
            selected = questionary.checkbox(
                "Categories (space=toggle, enter=confirm):",
                choices=[questionary.Choice(c, checked=(c in current)) for c in VALID_CATEGORIES],
            ).ask()
            if selected is not None:
                save("filters", "categories", ", ".join(selected), "categories")

        elif a.startswith("Regions"):
            current_lower = [r.strip().lower() for r in get("filters", "regions", "").split(",") if r.strip()]
            selected = questionary.checkbox(
                "Regions (space=toggle, enter=confirm):",
                choices=[questionary.Choice(r, checked=(r.lower() in current_lower)) for r in VALID_REGIONS],
            ).ask()
            if selected is not None:
                save("filters", "regions", ", ".join(selected), "regions")

        elif a.startswith("Themes"):
            current = [t.strip() for t in get("filters", "themes", "").split(",") if t.strip()]
            selected = questionary.checkbox(
                "Themes (space=toggle, enter=confirm):",
                choices=[questionary.Choice(t, checked=(t in current)) for t in VALID_THEMES],
            ).ask()
            if selected is not None:
                save("filters", "themes", ", ".join(selected), "themes")

        elif a.startswith("Media"):
            current = [m.strip() for m in get("filters", "media", "").split(",") if m.strip()]
            selected = questionary.checkbox(
                "Media (space=toggle, enter=confirm):",
                choices=[questionary.Choice(m, checked=(m in current)) for m in VALID_MEDIA],
            ).ask()
            if selected is not None:
                save("filters", "media", ", ".join(selected), "media")

        elif a.startswith("Date range"):
            for label, key, default in [
                ("Earliest year (negative for BCE, e.g. -500):", "date_min", "1400"),
                ("Latest year:", "date_max", "1900"),
            ]:
                v = questionary.text(
                    label,
                    default=get("filters", key, default),
                    validate=lambda v: v.lstrip("-").isdigit() or "Must be a year, e.g. 1400 or -500",
                ).ask()
                if v:
                    save("filters", key, v, key)

        elif a.startswith("Overlay enabled"):
            v = questionary.confirm(
                "Show metadata overlay on wallpaper?",
                default=get("overlay", "enabled", "true") == "true",
            ).ask()
            if v is not None:
                save("overlay", "enabled", str(v).lower(), "enabled")

        elif a.startswith("Position"):
            v = questionary.select(
                "Overlay card position:",
                choices=VALID_POSITIONS + ["-- Back (no change) --"],
                default=get("overlay", "position", "bottom-left"),
            ).ask()
            if v and v != "-- Back (no change) --":
                save("overlay", "position", v, "position")

        elif a.startswith("Font size"):
            v = questionary.text(
                "Font size in points  (leave blank to go back):",
                default=get("overlay", "font_size", "28"),
                validate=lambda v: v.isdigit() or "Must be a whole number",
            ).ask()
            if v:
                save("overlay", "font_size", v, "font_size")

        elif a.startswith("Background opacity"):
            v = questionary.text(
                "Background opacity 0.0-1.0  (leave blank to go back):",
                default=get("overlay", "background_opacity", "0.65"),
                validate=lambda v: (
                    v.replace(".", "", 1).isdigit() and 0.0 <= float(v) <= 1.0
                ) or "Must be a number between 0.0 and 1.0",
            ).ask()
            if v:
                save("overlay", "background_opacity", v, "background_opacity")

        elif a.startswith("Scaling"):
            # TODO (v2 GUI): becomes a dropdown in the settings interface
            v = questionary.select(
                "Wallpaper scaling mode:",
                choices=[
                    questionary.Choice("scaled     fit whole image, letterboxed (recommended for art)", value="scaled"),
                    questionary.Choice("zoom       crop to fill screen (may cut off overlay card)",    value="zoom"),
                    questionary.Choice("stretched  distort to fill screen",                           value="stretched"),
                    questionary.Choice("centered   center at native size, no scaling",                value="centered"),
                    questionary.Choice("wallpaper  tile the image",                                   value="wallpaper"),
                    questionary.Choice("-- Back (no change) --",                                      value="__back__"),
                ],
                default=get("overlay", "scaling", "scaled"),
            ).ask()
            if v and v != "__back__":
                save("overlay", "scaling", v, "scaling")

        elif a.startswith("Show museum name"):
            v = questionary.confirm(
                "Show museum name in the overlay card?",
                default=get("overlay", "show_source", "true") == "true",
            ).ask()
            if v is not None:
                save("overlay", "show_source", str(v).lower(), "show_source")

        elif a.startswith("Show artwork facts"):
            v = questionary.confirm(
                "Show a brief fact about the artwork or artist in the overlay?\n"
                "  (Met: period + themes, AIC: description, Rijksmuseum: title context)",
                default=get("overlay", "show_facts", "false") == "true",
            ).ask()
            if v is not None:
                save("overlay", "show_facts", str(v).lower(), "show_facts")

        print()


# ── Source registry ───────────────────────────────────────────────────────────

def build_source(name: str, cfg: configparser.ConfigParser, verbose: bool = False):
    """
    Instantiate and return a museum source object by name.
    Returns None if the source cannot be initialised (e.g. missing API key).
    """
    rk_key = cfg.get("sources", "rijksmuseum_api_key", fallback="")

    if name == "met":
        return MetSource()
    if name == "aic":
        return AICSource()
    if name == "rijksmuseum":
        if not rk_key or rk_key.strip() == "YOUR_KEY_HERE":
            warn("Rijksmuseum API key not configured -- skipping")
            return None
        return RijksmuseumSource(rk_key.strip())
    return None


def select_source(cfg: configparser.ConfigParser, forced: Optional[str], verbose: bool):
    """
    Select a source to use.
    If forced is set, use that source (or exit if it cannot be initialised).
    Otherwise, build a weighted pool from all enabled sources and pick randomly.
    """
    if forced:
        src = build_source(forced, cfg, verbose)
        if src is None:
            err(f"Cannot initialise source '{forced}'")
            sys.exit(1)
        return src

    enabled = {
        "met":         cfg.getboolean("sources", "met_museum",            fallback=True),
        "aic":         cfg.getboolean("sources", "art_institute_chicago", fallback=True),
        "rijksmuseum": cfg.getboolean("sources", "rijksmuseum",           fallback=False),
    }
    weights = {
        "met":         cfg.getint("sources", "met_weight",         fallback=1),
        "aic":         cfg.getint("sources", "aic_weight",         fallback=1),
        "rijksmuseum": cfg.getint("sources", "rijksmuseum_weight", fallback=1),
    }

    pool = []
    for name, is_enabled in enabled.items():
        if not is_enabled:
            continue
        src = build_source(name, cfg, verbose)
        if src is not None:
            pool.extend([src] * max(weights.get(name, 1), 1))

    if not pool:
        err("No museum sources are enabled or available in config.ini")
        sys.exit(1)

    chosen = random.choice(pool)
    dbg(f"Selected source: {chosen.NAME}", verbose)
    return chosen


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="artwall",
        description="artwall -- Rotate desktop wallpaper with museum artwork",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            examples:
              artwall.py                   fetch and set a new wallpaper
              artwall.py --dry-run         fetch, composite, but do not set wallpaper
              artwall.py --source aic      force Art Institute of Chicago as source
              artwall.py --info            show info about the current wallpaper
              artwall.py --verbose         show detailed debug output
              artwall.py --config PATH     use a non-default config file

            filter options are configured in config.ini
            see config.ini.example for the full reference
        """),
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config.ini (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and composite the image but do not set it as wallpaper",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed debug output including API parameters",
    )
    parser.add_argument(
        "--info", action="store_true",
        help="Print metadata about the current wallpaper and exit",
    )
    parser.add_argument(
        "--source", choices=["met", "aic", "rijksmuseum"], default=None,
        help="Force a specific museum source instead of random selection",
    )
    parser.add_argument(
        "--version", action="version", version=f"artwall {VERSION}",
    )
    parser.add_argument(
        "--configure", action="store_true",
        help="Launch the interactive configuration editor",
    )
    args = parser.parse_args()

    print()
    print(f"  {clr(CYAN, 'artwall')} {VERSION}")
    print()

    # ── Preflight: verify dependencies before any work (Rule 15)
    preflight()

    # ── Early exit: interactive configuration editor
    if args.configure:
        config_path = Path(args.config).expanduser().resolve()
        configure(config_path)
        return

    # ── Step 1: load configuration
    step(1, TOTAL_STEPS, "Loading configuration")
    config_path = Path(args.config).expanduser().resolve()
    cfg         = load_config(config_path)
    filters     = load_filters(cfg)

    cache_dir   = Path(cfg.get("general", "cache_dir",   fallback="~/.cache/artwall")).expanduser()
    output_path = Path(cfg.get("general", "output_path", fallback="~/.cache/artwall/current.jpg")).expanduser()
    cache_max   = cfg.getint("general", "cache_max", fallback=100)

    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    dbg(f"Config:      {config_path}", args.verbose)
    dbg(f"Cache dir:   {cache_dir}", args.verbose)
    dbg(f"Output path: {output_path}", args.verbose)
    dbg(
        f"Filters: categories={filters.categories} regions={filters.regions} "
        f"themes={filters.themes} media={filters.media} "
        f"date={filters.date_min}-{filters.date_max}",
        args.verbose,
    )

    if args.info:
        show_info(cache_dir)
        return

    ok(f"Config loaded from {config_path}")

    # ── Steps 2 and 3: fetch artwork and download image
    # Re-selects the source on each retry attempt so a persistent download
    # failure from one museum (e.g. AIC CDN 403) may resolve by switching
    # to a different source on the next attempt.
    MAX_FETCH_ATTEMPTS = 3
    artwork  = None
    raw_path = None

    step(2, TOTAL_STEPS, "Fetching artwork from museum API")
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        if attempt > 1:
            warn(f"Retrying with a different source (attempt {attempt}/{MAX_FETCH_ATTEMPTS})")

        source  = select_source(cfg, args.source, args.verbose)

        artwork = source.fetch_random(filters, verbose=args.verbose)
        if artwork is None:
            err("Could not fetch artwork -- adjust your filters or check network")
            sys.exit(1)

        ok(
            f"Found: {artwork.title!r}"
            f"{(' by ' + artwork.artist) if artwork.artist else ''}"
            f"{(' (' + artwork.year + ')') if artwork.year else ''}"
        )
        dbg(f"Image URL: {artwork.image_url}", args.verbose)

        step(3, TOTAL_STEPS, "Downloading image")
        timestamp = int(time.time())
        raw_path  = cache_dir / f"raw_{timestamp}.jpg"

        if download_image(artwork.image_url, raw_path, verbose=args.verbose):
            size_kb = raw_path.stat().st_size // 1024
            ok(f"Downloaded {size_kb} KB")
            break

        raw_path.unlink(missing_ok=True)
    else:
        err("Image download failed after multiple attempts -- check network or broaden filters")
        sys.exit(1)

    # ── Step 4: composite metadata overlay
    step(4, TOTAL_STEPS, "Compositing metadata overlay")

    overlay_enabled = cfg.getboolean("overlay", "enabled",            fallback=True)
    position        = cfg.get      ("overlay", "position",            fallback="bottom-left")
    font_size       = cfg.getint   ("overlay", "font_size",           fallback=40)
    bg_opacity      = cfg.getfloat ("overlay", "background_opacity",  fallback=0.65)
    padding         = cfg.getint   ("overlay", "padding",             fallback=20)
    show_source     = cfg.getboolean("overlay", "show_source",  fallback=True)
    show_facts      = cfg.getboolean("overlay", "show_facts",   fallback=False)
    scaling         = cfg.get      ("overlay", "scaling",       fallback="scaled")
    text_color_raw  = cfg.get      ("overlay", "text_color",   fallback="255, 255, 255")
    text_color      = tuple(int(x.strip()) for x in text_color_raw.split(","))

    if overlay_enabled:
        success = composite_overlay(
            image_path  = raw_path,
            output_path = output_path,
            artwork     = artwork,
            position    = position,
            font_size   = font_size,
            bg_opacity  = bg_opacity,
            padding     = padding,
            text_color  = text_color,
            show_source = show_source,
            show_facts  = show_facts,
            scaling     = scaling,
            verbose     = args.verbose,
        )
        if not success:
            warn("Overlay compositing failed -- using raw image without overlay")
            shutil.copy(raw_path, output_path)
    else:
        shutil.copy(raw_path, output_path)

    # Persist metadata so --info can display it later
    meta_path = cache_dir / "current.json"
    with open(meta_path, "w") as fh:
        json.dump({
            "title":       artwork.title,
            "artist":      artwork.artist,
            "year":        artwork.year,
            "medium":      artwork.medium,
            "culture":     artwork.culture,
            "source_name": artwork.source_name,
            "artwork_url": artwork.artwork_url,
            "image_url":   artwork.image_url,
            "fact":        artwork.fact,
            "timestamp":   timestamp,
        }, fh, indent=2)

    ok(f"Composited image ready at {output_path}")

    # ── Step 5: set wallpaper
    step(5, TOTAL_STEPS, "Setting wallpaper")

    if args.dry_run:
        warn("Dry-run mode: wallpaper not set")
        print(f"        Image is at: {output_path}")
    else:
        if set_wallpaper(output_path, scaling=scaling, verbose=args.verbose):
            ok("Wallpaper set")
        else:
            warn("Could not set wallpaper automatically")
            print(f"        Set it manually to: {output_path}")

    trim_cache(cache_dir, cache_max, verbose=args.verbose)

    print()
    print(f"  {clr(GREEN, 'Done.')}  {artwork.title!r}")
    if artwork.artwork_url:
        print(f"          {clr(CYAN, artwork.artwork_url)}")
    print()


if __name__ == "__main__":
    main()
