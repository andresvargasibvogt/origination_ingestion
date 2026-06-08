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

from .config import load_settings
from .manifest import ATTRIBUTION, SCHEMA_VERSION, SOURCE, FailedItem, ItemEntry, Manifest, RunInfo, now_iso
from .onelake import OneLakeWriter, select_credential
from .paths import manifest_path

log = structlog.get_logger()

# Tag key used by Defender for Storage. Microsoft writes the key as
# `Malware Scanning scan result` (singular) — confirmed against
# learn.microsoft.com and against tags we see in the portal.
SCAN_RESULT_TAG = "Malware Scanning scan result"
SCAN_VERDICT_CLEAN = "No threats found"
SCAN_VERDICT_MALICIOUS = "Malicious"

# Path pattern: bronze/boe/raw/year=YYYY/month=MM/day=DD/<rest>
_DATE_PATH_RE = re.compile(
    r"^bronze/(?P<source>[^/]+)/raw/year=(?P<year>\d{4})/month=(?P<month>\d{2})/day=(?P<day>\d{2})/(?P<rest>.+)$"
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
            section=metadata["section"],
            departamento_codigo=metadata["departamento_codigo"],
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
) -> Manifest:
    """Merge `new_items` into the existing manifest by identifier (last write wins)."""
    if existing is None:
        run = RunInfo(
            started_at=now_iso(),
            ended_at=now_iso(),
            date=target_date_iso,
            sumario_items_total=0,           # unknown to the promoter; left at 0 — informational only
            items_filtered_in=len(new_items),
            items_written=len(new_items),
            items_failed=failed,
            attribution=ATTRIBUTION,
        )
        return Manifest(
            schema_version=SCHEMA_VERSION,
            source=SOURCE,
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
    settings = load_settings()

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

    promoted_by_day: dict[str, list[ItemEntry]] = defaultdict(list)
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
        _source, year, month, day, filename = parts
        date_iso = f"{year}-{month}-{day}"

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
            promoted_by_day[date_iso].append(entry)
        blob_client.delete_blob()
        log.info("blob_promoted", path=path, bytes=len(data))

    # Upsert manifests for any dates we touched
    for date_iso, new_items in promoted_by_day.items():
        year, month, day = date_iso.split("-")
        from datetime import date as _date  # local import to keep module top tidy
        manifest_rel_path = manifest_path(_date(int(year), int(month), int(day)))
        existing = _load_existing_manifest(onelake, manifest_rel_path)
        merged = _upsert_manifest(existing, date_iso, new_items, failed=[])
        onelake.write_text(manifest_rel_path, merged.to_json())
        log.info(
            "manifest_upserted",
            date=date_iso,
            items_added=len(new_items),
            items_total=len(merged.items),
        )

    log.info(
        "promoter_run_done",
        sumarios_copied=sumarios_copied,
        promoted=sum(len(v) for v in promoted_by_day.values()),
        quarantined=quarantined,
        pending=pending,
        skipped=skipped,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
