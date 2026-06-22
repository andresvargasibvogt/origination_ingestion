"""Discover the latest e-distribución generation-capacity CSV from the landing page.

The page lists each month's downloads as static hrefs, e.g.:

    /content/dam/edistribucion/.../generacion/202606/2026_06_03_R1299_generación.csv

Two complications the regex/selection must handle:

  1. Two parallel series share the filename shape — "e-distribución" (wanted)
     and "EASA" (excluded). They differ by the visible link text, so we select
     by text (config.SERIES_TEXT_MARKER), not by the internal R-code.
  2. The filename suffix is spelled `generación.csv` (accented) on recent months
     and `generacion.csv` (no accent) on older ones — we accept both.

No SPA, no JS — a plain HTTP GET of the page is enough.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import httpx
import structlog

from .config import LANDING_PAGE_PATH, SERIES_EXCLUDE_MARKER, SERIES_TEXT_MARKER

log = structlog.get_logger()

# Anchor whose href is a generation CSV. Captures (href, inner_html). The
# filename accepts both `generación` and `generacion` (accent drift across months).
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href="(?P<href>[^"]*generaci[oó]n\.csv)"[^>]*>(?P<text>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
# Publication date from the filename prefix: YYYY_MM_DD_...
_DATE_RE = re.compile(r"/(?P<y>\d{4})_(?P<m>\d{2})_(?P<d>\d{2})_[^/]*generaci[oó]n\.csv$", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


class DiscoveryError(Exception):
    """Raised when no e-distribución CSV link can be found on the landing page."""


@dataclass(frozen=True)
class LatestFile:
    url_path: str          # site-relative path, e.g. /content/dam/.../2026_06_03_R1299_generación.csv
    filename: str          # 2026_06_03_R1299_generación.csv
    published_at: date     # parsed from the filename prefix


async def fetch_landing_page(client: httpx.AsyncClient) -> str:
    log.info("endesa_landing_fetch_start", path=LANDING_PAGE_PATH)
    resp = await client.get(LANDING_PAGE_PATH)
    resp.raise_for_status()
    log.info("endesa_landing_fetch_ok", bytes=len(resp.content))
    return resp.text


def _parse_date(href: str) -> date | None:
    m = _DATE_RE.search(href)
    if not m:
        return None
    try:
        return date(int(m["y"]), int(m["m"]), int(m["d"]))
    except ValueError:
        return None


def find_latest_csv(html_text: str) -> LatestFile:
    """Return the most-recent *e-distribución* generation CSV link in `html_text`.

    Selects by link text (the "e-distribución" series, excluding "EASA"), so it
    is robust to the internal R-code changing. Raises DiscoveryError if none
    match (page reskin / series renamed), so a structural change surfaces loudly
    rather than silently ingesting nothing.
    """
    # An href can appear in several anchors (descriptive text + a "CSV(NNN)KB"
    # link), so accumulate all link text seen per href before classifying.
    text_by_href: dict[str, str] = defaultdict(str)
    for m in _ANCHOR_RE.finditer(html_text):
        inner = html.unescape(_TAG_RE.sub("", m["text"]))
        text_by_href[m["href"]] += " " + inner.lower()

    candidates: list[LatestFile] = []
    excluded = 0
    for href, text in text_by_href.items():
        if SERIES_TEXT_MARKER not in text or SERIES_EXCLUDE_MARKER in text:
            excluded += 1
            continue
        pub = _parse_date(href)
        if pub is None:
            log.warning("endesa_bad_date_in_filename", href=href)
            continue
        filename = href.rsplit("/", 1)[-1]
        candidates.append(LatestFile(url_path=href, filename=filename, published_at=pub))

    if not candidates:
        raise DiscoveryError(
            "No 'e-distribución' generation CSV link found on the landing page "
            "(page structure or series naming may have changed)"
        )

    latest = max(candidates, key=lambda c: c.published_at)
    log.info(
        "endesa_latest_discovered",
        filename=latest.filename,
        published_at=latest.published_at.isoformat(),
        candidates=len(candidates),
        excluded=excluded,
    )
    return latest
