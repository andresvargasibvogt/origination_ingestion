"""Discover the latest REE capacity CSV from the landing page.

The page lists the file as a static href:

    /sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.csv

We extract every href ending in `_GRT_generacion.csv`, parse the publication
date from the `YYYY_MM_DD` prefix of each filename, and return the most recent.
No SPA, no JS — a plain HTTP GET of the page is enough.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import httpx
import structlog

from .config import LANDING_PAGE_PATH, TARGET_FILENAME_SUFFIX

log = structlog.get_logger()

# Matches hrefs like:
#   /sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.csv
# Capturing the path and the YYYY_MM_DD date prefix of the filename.
_HREF_RE = re.compile(
    r'href="(?P<path>[^"]*/(?P<y>\d{4})_(?P<m>\d{2})_(?P<d>\d{2})'
    + re.escape(TARGET_FILENAME_SUFFIX)
    + r')"',
    re.IGNORECASE,
)


class DiscoveryError(Exception):
    """Raised when no target CSV link can be found on the landing page."""


@dataclass(frozen=True)
class LatestFile:
    url_path: str          # site-relative path, e.g. /sites/default/files/.../2026_06_04_GRT_generacion.csv
    filename: str          # 2026_06_04_GRT_generacion.csv
    published_at: date     # parsed from the filename prefix


async def fetch_landing_page(client: httpx.AsyncClient) -> str:
    log.info("ree_landing_fetch_start", path=LANDING_PAGE_PATH)
    resp = await client.get(LANDING_PAGE_PATH)
    resp.raise_for_status()
    log.info("ree_landing_fetch_ok", bytes=len(resp.content))
    return resp.text


def find_latest_csv(html: str) -> LatestFile:
    """Return the most-recent `*_GRT_generacion.csv` link found in `html`.

    Raises DiscoveryError if none match (e.g. page reskin or the file was
    pulled). Picks the max publication date if several are present.
    """
    candidates: list[LatestFile] = []
    for m in _HREF_RE.finditer(html):
        path = m["path"]
        filename = path.rsplit("/", 1)[-1]
        try:
            pub = date(int(m["y"]), int(m["m"]), int(m["d"]))
        except ValueError:
            # A malformed date in the filename — skip this candidate.
            log.warning("ree_bad_date_in_filename", filename=filename)
            continue
        candidates.append(LatestFile(url_path=path, filename=filename, published_at=pub))

    if not candidates:
        raise DiscoveryError(
            f"No '*{TARGET_FILENAME_SUFFIX}' link found on the REE landing page "
            "(page structure may have changed)"
        )

    latest = max(candidates, key=lambda c: c.published_at)
    log.info(
        "ree_latest_discovered",
        filename=latest.filename,
        published_at=latest.published_at.isoformat(),
        candidates=len(candidates),
    )
    return latest
