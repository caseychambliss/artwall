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

    def label_lines(self, show_source: bool = True) -> list[str]:
        """
        Return ordered lines of text for the metadata overlay card.

        Line order:
          1. Title (always present; falls back to "Untitled")
          2. Artist (omitted if unknown)
          3. Year  |  Medium  (omitted if both are unknown)
          4. Source name (optional, controlled by show_source)
        """
        lines = [self.title or "Untitled"]
        if self.artist:
            lines.append(self.artist)
        detail = "  |  ".join(p for p in (self.year, self.medium) if p)
        if detail:
            lines.append(detail)
        if show_source and self.source_name:
            lines.append(self.source_name)
        return lines


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
            r = requests.get(f"{self.BASE}/search", params=params, timeout=10)
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
            r = requests.get(f"{self.BASE}/objects/{object_id}", timeout=10)
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
        )


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
    "is_public_domain"
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
            r = requests.get(f"{self.BASE}/artworks", params=params, timeout=10)
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
            artist      = item.get("artist_display", ""),
            year        = item.get("date_display", ""),
            medium      = item.get("medium_display", ""),
            culture     = item.get("place_of_origin", ""),
            image_url   = image_url,
            source_name = self.NAME,
            artwork_url = artwork_url,
        )


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
            r = requests.get(self.BASE, params=params, timeout=10)
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
        )


# ── Image download ────────────────────────────────────────────────────────────

def download_image(url: str, dest: Path, verbose: bool = False) -> bool:
    """
    Stream-download an image from url to dest.
    Returns True on success, False on any network or HTTP error.
    """
    import requests
    try:
        dbg(f"Downloading: {url}", verbose)
        r = requests.get(url, stream=True, timeout=30)
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


def find_system_font(size: int):
    """
    Attempt to load a readable Bold sans-serif font from common Linux paths.
    Falls back to Pillow's built-in bitmap default if none are found.
    The built-in font ignores the size argument and renders small but is
    always available.
    """
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


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
    verbose:     bool = False,
) -> bool:
    """
    Open the image at image_path, composite a semi-transparent metadata card
    containing the artwork's label lines, and save the result as a JPEG at
    output_path.

    The card dimensions adapt to the text content. Line 0 (title) uses
    font_size; subsequent lines use 80% of font_size.

    Returns True on success, False if the image cannot be opened or saved.
    """
    from PIL import Image, ImageDraw

    try:
        img = Image.open(image_path).convert("RGBA")
    except Exception as exc:
        err(f"Cannot open image for compositing: {exc}")
        return False

    lines = artwork.label_lines(show_source=show_source)
    if not lines:
        shutil.copy(image_path, output_path)
        return True

    detail_size = max(int(font_size * 0.8), 12)
    font_title  = find_system_font(font_size)
    font_detail = find_system_font(detail_size)
    fonts = [font_title] + [font_detail] * (len(lines) - 1)

    # Measure each line with a throw-away draw context
    dummy_draw = ImageDraw.Draw(img)
    bboxes = [dummy_draw.textbbox((0, 0), line, font=f) for line, f in zip(lines, fonts)]
    line_heights = [b[3] - b[1] for b in bboxes]
    line_widths  = [b[2] - b[0] for b in bboxes]
    line_spacing = int(max(line_heights) * 0.35)

    text_w = max(line_widths)
    text_h = sum(line_heights) + line_spacing * (len(lines) - 1)
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
    for line, font, lh in zip(lines, fonts, line_heights):
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


def set_wallpaper(image_path: Path, verbose: bool = False) -> bool:
    """
    Set the desktop wallpaper to image_path.

    Supports Cinnamon, GNOME, MATE, XFCE, KDE Plasma, and makes a best-effort
    attempt for unknown environments via gsettings.

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
        ],
        "gnome": [
            ["gsettings", "set", "org.gnome.desktop.background",
             "picture-uri", uri],
            ["gsettings", "set", "org.gnome.desktop.background",
             "picture-uri-dark", uri],
        ],
        "mate": [
            ["gsettings", "set", "org.mate.background",
             "picture-filename", path_str],
        ],
        "budgie": [
            ["gsettings", "set", "org.gnome.desktop.background",
             "picture-uri", uri],
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
    args = parser.parse_args()

    print()
    print(f"  {clr(CYAN, 'artwall')} {VERSION}")
    print()

    # ── Preflight: verify dependencies before any work (Rule 15)
    preflight()

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

    # ── Step 2: fetch artwork from museum API
    step(2, TOTAL_STEPS, "Fetching artwork from museum API")
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

    # ── Step 3: download image (Rule 19: timestamped filename avoids
    #   overwriting a cached file we might want to keep)
    step(3, TOTAL_STEPS, "Downloading image")
    timestamp = int(time.time())
    raw_path  = cache_dir / f"raw_{timestamp}.jpg"

    if not download_image(artwork.image_url, raw_path, verbose=args.verbose):
        err("Image download failed")
        sys.exit(1)

    size_kb = raw_path.stat().st_size // 1024
    ok(f"Downloaded {size_kb} KB")

    # ── Step 4: composite metadata overlay
    step(4, TOTAL_STEPS, "Compositing metadata overlay")

    overlay_enabled = cfg.getboolean("overlay", "enabled",            fallback=True)
    position        = cfg.get      ("overlay", "position",            fallback="bottom-left")
    font_size       = cfg.getint   ("overlay", "font_size",           fallback=28)
    bg_opacity      = cfg.getfloat ("overlay", "background_opacity",  fallback=0.65)
    padding         = cfg.getint   ("overlay", "padding",             fallback=20)
    show_source     = cfg.getboolean("overlay", "show_source",        fallback=True)
    text_color_raw  = cfg.get      ("overlay", "text_color",          fallback="255, 255, 255")
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
            "timestamp":   timestamp,
        }, fh, indent=2)

    ok(f"Composited image ready at {output_path}")

    # ── Step 5: set wallpaper
    step(5, TOTAL_STEPS, "Setting wallpaper")

    if args.dry_run:
        warn("Dry-run mode: wallpaper not set")
        print(f"        Image is at: {output_path}")
    else:
        if set_wallpaper(output_path, verbose=args.verbose):
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
