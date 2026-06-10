"""Promoter — moves clean blobs from staging to OneLake (ADR-008 Pattern F).

Runs every 5 minutes as a separate ACA Job (`caj-promoter`).

For each blob in the staging container `untrusted/`:

  1. Read its blob index tag `Malware Scanning scan results`
     - "No threats found" → copy bytes to OneLake at the same path,
       upsert the day's manifest in OneLake, then delete the staging blob.
     - "Malicious"        → move blob to `quarantine/`, emit a security
       log event. Do not promote.
     - Missing/pending    → skip; will retry next run.

  2. Sumario JSON (no scan needed but treated the same way) — copied to
     OneLake when seen.

The promoter is idempotent and crash-safe:
  - "Copy then delete" semantics: if the promoter dies between copy and
    delete, the next run sees the blob still in staging, re-copies
    (overwriting with identical bytes), and tries the delete again.
  - Manifest upserts merge per-item records by `identifier`, so re-running
    after a partial promotion just no-ops the already-promoted items.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import defaultdict
from typing import Any

import structlog
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobClient, BlobServiceClient
from pydantic import TypeAdapter, ValidationError

from .config import load_common_settings
from .manifest import (
    FailedItem,
    ItemEntry,
    Manifest,
    RunInfo,
    Source,
    attribution_for,
    now_iso,
)
from .onelake import OneLakeWriter, select_credential
from .paths import manifest_path

log = structlog.get_logger()

# Single source of truth for "is this a known Source literal value?"
# Updates automatically when the Source Literal in manifest.py is widened.
_SOURCE_ADAPTER: TypeAdapter[Source] = TypeAdapter(Source)

# Tag key used by Defender for Storage. Microsoft writes the key as
# `Malware Scanning scan result` (singular) — confirmed against
# learn.microsoft.com and against tags we see in the portal.
SCAN_RESULT_TAG = "Malware Scanning scan result"
SCAN_VERDICT_CLEAN = "No threats found"
SCAN_VERDICT_MALICIOUS = "Malicious"

# Path pattern the promoter must read back for EVERY source. The day= segment
# is optional so the one shared promoter can parse both partition shapes:
#   daily   sources (BOE, BOA): bronze/{source}/raw/year=YYYY/month=MM/day=DD/<rest>
#   monthly source  (REE):      bronze/{source}/raw/year=YYYY/month=MM/<rest>
# BOE/BOA paths still contain day= and match exactly as before — this only
# ADDS the ability to also recognize REE's month-level path.
_DATE_PATH_RE = re.compile(
    r"^bronze/(?P<source>[^/]+)/raw/year=(?P<year>\d{4})/month=(?P<month>\d{2})"
    r"(?:/day=(?P<day>\d{2}))?/(?P<rest>.+)$"
)


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def _read_scan_verdict(blob_client: BlobClient) -> str | None:
    """Return the Defender scan verdict or None if not yet tagged."""
    try:
        tags = blob_client.get_blob_tags() or {}
    except ResourceNotFoundError:
        return None
    return tags.get(SCAN_RESULT_TAG)


def _parse_date_path(path: str) -> tuple[str, str, str, str, str] | None:
    """Split a `bronze/{source}/raw/year=.../day=.../filename` path into parts.

    Returns (source, year, month, day, filename) or None if the path
    doesn't match the expected layout.
    """
    m = _DATE_PATH_RE.match(path)
    if not m:
        return None
    return m["source"], m["year"], m["month"], m["day"], m["rest"]


def _blob_metadata_to_item_entry(
    metadata: dict[str, str], lakehouse_path: str
) -> ItemEntry | None:
    """Convert blob metadata into a manifest ItemEntry. Returns None on bad metadata."""
    try:
        return ItemEntry(
            identifier=metadata["identifier"],
            section=metadata.get("section", ""),
            subsection=metadata.get("subsection"),
            departamento_codigo=metadata.get("departamento_codigo", ""),
            departamento=metadata.get("departamento", ""),
            published_at=metadata["published_at"],
            url_pdf=metadata.get("url_pdf"),
            url_xml=metadata.get("url_xml"),
            url_html=metadata.get("url_html"),
            eli=None,
            pdf_path=lakehouse_path,
            sha256=metadata["sha256"],
            size_bytes=int(metadata["size_bytes"]),
        )
    except (KeyError, ValueError) as exc:
        log.warning("manifest_metadata_invalid", path=lakehouse_path, error=str(exc))
        return None


def _load_existing_manifest(onelake: OneLakeWriter, manifest_lakehouse_path: str) -> Manifest | None:
    """Try to read an existing OneLake manifest. None if it doesn't exist yet."""
    try:
        full_path = f"{onelake._lakehouse_files_prefix}/{manifest_lakehouse_path.lstrip('/')}"  # noqa: SLF001
        file_client = onelake._service.get_file_client(  # noqa: SLF001
            file_system=onelake._workspace,  # noqa: SLF001
            file_path=full_path,
        )
        data = file_client.download_file().readall()
        return Manifest.model_validate_json(data)
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        log.warning("manifest_load_failed", path=manifest_lakehouse_path, error=str(exc))
        return None


def _upsert_manifest(
    existing: Manifest | None,
    target_date_iso: str,
    new_items: list[ItemEntry],
    failed: list[FailedItem],
    source: Source,
) -> Manifest:
    """Merge `new_items` into the existing manifest by identifier (last write wins).

    `source` is the source identifier discovered from the blob path
    (`bronze/{source}/raw/...`), so a single promoter run handles BOE + BOA
    + any future source from a single image.
    """
    if existing is None:
        run = RunInfo(
            started_at=now_iso(),
            ended_at=now_iso(),
            date=target_date_iso,
            sumario_items_total=0,           # unknown to the promoter; left at 0 — informational only
            items_filtered_in=len(new_items),
            items_written=len(new_items),
            items_failed=failed,
            attribution=attribution_for(source),
        )
        return Manifest(
            source=source,
            run=run,
            items=list(new_items),
        )

    by_id: dict[str, ItemEntry] = {it.identifier: it for it in existing.items}
    for it in new_items:
        by_id[it.identifier] = it
    merged_items = list(by_id.values())

    run = existing.run.model_copy(update={
        "ended_at": now_iso(),
        "items_written": len(merged_items),
        "items_failed": existing.run.items_failed + failed,
    })
    return Manifest(
        schema_version=existing.schema_version,
        source=existing.source,
        run=run,
        items=merged_items,
    )


def main() -> int:
    _configure_logging()
    settings = load_common_settings()

    if not settings.stg_account_name:
        log.error("STG_ACCOUNT_NAME not set — promoter cannot run")
        return 1
    if not settings.fabric_workspace_name:
        log.error("FABRIC_WORKSPACE_NAME not set — promoter cannot run")
        return 1

    cred = select_credential(settings.azure_client_id)
    account_url = f"https://{settings.stg_account_name}.blob.core.windows.net"
    stg = BlobServiceClient(account_url=account_url, credential=cred)

    untrusted = stg.get_container_client(settings.stg_container_untrusted)
    quarantine = stg.get_container_client(settings.stg_container_quarantine)

    onelake = OneLakeWriter(
        workspace_name=settings.fabric_workspace_name,
        lakehouse_name=settings.fabric_lakehouse_name,
        azure_client_id=settings.azure_client_id,
    )

    log.info(
        "promoter_run_start",
        account=settings.stg_account_name,
        container=settings.stg_container_untrusted,
    )

    # Keyed by (source, year, month, day) — one manifest per partition folder.
    # `day` is None for monthly sources (REE), a string for daily ones (BOE/BOA),
    # so daily and monthly partitions each get their own manifest. One promoter
    # run handles every source by routing on the source segment of the path.
    promoted_by_key: dict[tuple[Source, str, str, str | None], list[ItemEntry]] = defaultdict(list)
    quarantined: int = 0
    pending: int = 0
    skipped: int = 0
    sumarios_copied: int = 0

    for blob in untrusted.list_blobs(include=["metadata"]):
        path = blob.name
        blob_client = untrusted.get_blob_client(path)
        parts = _parse_date_path(path)
        if parts is None:
            log.warning("path_unparseable", path=path)
            skipped += 1
            continue
        source_raw, year, month, day, filename = parts  # day may be None (monthly source)
        try:
            source: Source = _SOURCE_ADAPTER.validate_python(source_raw)
        except ValidationError:
            log.warning("unknown_source", path=path, source=source_raw)
            skipped += 1
            continue
        date_iso = f"{year}-{month}-{day}" if day else f"{year}-{month}"

        # Sumario JSON: copy as-is, no verdict check needed (it's JSON, not user content)
        if filename == "sumario.json":
            data = blob_client.download_blob().readall()
            onelake.write_bytes(path, data)
            blob_client.delete_blob()
            sumarios_copied += 1
            log.info("sumario_promoted", path=path)
            continue

        # PDFs: check Defender's verdict
        verdict = _read_scan_verdict(blob_client)
        if verdict is None:
            pending += 1
            log.info("scan_pending", path=path)
            continue

        if verdict == SCAN_VERDICT_MALICIOUS:
            # Copy to quarantine container, then delete from untrusted.
            quarantine_blob = quarantine.get_blob_client(path)
            src_url = blob_client.url
            quarantine_blob.start_copy_from_url(src_url)
            blob_client.delete_blob()
            quarantined += 1
            log.warning("scan_malicious", path=path, verdict=verdict)
            continue

        if verdict != SCAN_VERDICT_CLEAN:
            log.warning("scan_unknown_verdict", path=path, verdict=verdict)
            skipped += 1
            continue

        # Clean blob → promote to OneLake
        metadata = blob_client.get_blob_properties().metadata or {}
        data = blob_client.download_blob().readall()
        onelake.write_bytes(path, data)
        entry = _blob_metadata_to_item_entry(metadata, path)
        if entry is not None:
            promoted_by_key[(source, year, month, day)].append(entry)
        blob_client.delete_blob()
        log.info("blob_promoted", path=path, bytes=len(data))

    # Upsert one manifest per touched partition. day present → daily manifest
    # (BOE/BOA); day absent → monthly manifest (REE). The RunInfo.date is the
    # representative date for the partition (item pub date for monthly).
    from datetime import date as _date  # local import to keep module top tidy
    for (source, year, month, day), new_items in promoted_by_key.items():
        if day is not None:
            d = _date(int(year), int(month), int(day))
            granularity = "day"
            date_iso = d.isoformat()
        else:
            d = _date(int(year), int(month), 1)
            granularity = "month"
            date_iso = new_items[0].published_at  # true pub date (e.g. 2026-06-04)
        manifest_rel_path = manifest_path(d, source=source, granularity=granularity)
        existing = _load_existing_manifest(onelake, manifest_rel_path)
        merged = _upsert_manifest(existing, date_iso, new_items, failed=[], source=source)
        onelake.write_text(manifest_rel_path, merged.to_json())
        log.info(
            "manifest_upserted",
            source=source,
            date=date_iso,
            granularity=granularity,
            items_added=len(new_items),
            items_total=len(merged.items),
        )

    log.info(
        "promoter_run_done",
        sumarios_copied=sumarios_copied,
        promoted=sum(len(v) for v in promoted_by_key.values()),
        quarantined=quarantined,
        pending=pending,
        skipped=skipped,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
