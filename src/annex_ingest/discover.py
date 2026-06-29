"""Discover annex links inside BOE announcements.

The substantive annex links live in each announcement's XML body (already
recorded per item as `url_xml` in the BOE manifest). We fetch that XML and
regex out the `almacen.redsara.es/sending/public/<uuid>` links. The
`rec.redsara.es` electronic-registry link (a submission portal, not a document)
is excluded by construction — the regex is anchored to the `almacen` host.

Two enumeration modes:
  - from the day's promoted BOE manifest (production: --date / --backfill)
  - from explicit announcement ids (ad-hoc/testing: --announcement)
"""

from __future__ import annotations

import re
from datetime import date

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from origination_common.fetcher import get_with_retry
from origination_common.manifest import Manifest
from origination_common.paths import manifest_path

from .config import BOE_XML_PATH

log = structlog.get_logger()

# Anchored to the almacen host + the canonical 8-4-4-4-12 UUID, so the
# rec.redsara.es registry host and the 40-hex legacy ssweb host don't match.
_ALMACEN_RE = re.compile(
    r"https?://almacen\.redsara\.es/sending/public/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
# Legacy SARA warehouse (older announcements) — DEFERRED in v1; plug-in point.
_SSWEB_RE = re.compile(r"https?://ssweb\.seap\.minhap\.es/almacen/descarga/envio/([0-9a-fA-F]{40})")

_PUB_DATE_RE = re.compile(r"<fecha_publicacion>\s*(\d{8})\s*</fecha_publicacion>")


class AnnouncementWork(BaseModel):
    model_config = ConfigDict(frozen=True)

    announcement_external_id: str
    url_xml: str
    published_at: date
    sending_uuids: tuple[str, ...]   # almacen sendings, de-duplicated, order preserved


def extract_sending_uuids(xml_text: str) -> list[str]:
    """Distinct almacen sending UUIDs in the announcement XML (order preserved)."""
    seen: dict[str, None] = {}
    for m in _ALMACEN_RE.finditer(xml_text):
        seen.setdefault(m.group(1), None)
    return list(seen)


def count_legacy_links(xml_text: str) -> int:
    """How many legacy ssweb links the announcement has (deferred — for visibility)."""
    return len(set(_SSWEB_RE.findall(xml_text)))


def extract_publication_date(xml_text: str) -> date | None:
    m = _PUB_DATE_RE.search(xml_text)
    if not m:
        return None
    raw = m.group(1)
    try:
        return date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def xml_url_for(announcement_id: str, base_url: str) -> str:
    return f"{base_url}{BOE_XML_PATH.format(identifier=announcement_id)}"


async def discover_one(
    client: httpx.AsyncClient,
    announcement_id: str,
    url_xml: str,
    *,
    fallback_date: date,
) -> AnnouncementWork:
    """Fetch one announcement's XML and extract its annex sending UUIDs."""
    resp = await get_with_retry(client, url_xml)
    resp.raise_for_status()
    xml_text = resp.text
    uuids = extract_sending_uuids(xml_text)
    legacy = count_legacy_links(xml_text)
    if legacy:
        log.info("annex_legacy_links_skipped", announcement=announcement_id, count=legacy)
    pub = extract_publication_date(xml_text) or fallback_date
    log.info("annex_discovered", announcement=announcement_id, sendings=len(uuids))
    return AnnouncementWork(
        announcement_external_id=announcement_id,
        url_xml=url_xml,
        published_at=pub,
        sending_uuids=tuple(uuids),
    )


def announcements_from_manifest(reader, target_date: date) -> list[tuple[str, str]]:
    """(identifier, url_xml) for every item in the day's promoted BOE manifest.

    `reader` must expose `read_text(lakehouse_path) -> str | None` (OneLakeWriter
    / LocalWriter). Returns [] when the day has no manifest (Sunday/holiday or
    not yet ingested).
    """
    path = manifest_path(target_date, source="boe", granularity="day")
    text = reader.read_text(path)
    if not text:
        log.info("annex_no_boe_manifest", date=target_date.isoformat(), path=path)
        return []
    manifest = Manifest.model_validate_json(text)
    return [(it.identifier, it.url_xml) for it in manifest.items if it.url_xml]
