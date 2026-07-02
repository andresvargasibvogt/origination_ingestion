"""Annex-promoter — drains scanned annex blobs from staging into OneLake.

Deliberately SEPARATE from the shared `origination_common.promoter`:
  - annex blobs use a non-Hive path (`bronze/{source}/annexes/…`) the shared
    promoter's regex doesn't match (and the shared promoter has a guard to skip
    them);
  - annex files are large (≤~880 MB) — the shared promoter buffers whole blobs
    in 0.5 GiB; this one STREAMS (chunked) via OneLakeWriter.stream_from_blob;
  - annexes track state in the package's JSONL state-manifest, not in the
    per-date `_manifest.json` the shared promoter writes.

Verdict handling reuses the shared Defender constants. Clean → stream-promote +
delete; Malicious → quarantine; missing tag → skip (retry next run); any other
verdict (e.g. scan timeout) → `scan_failed`, left in staging, surfaced to ops.
"""

from __future__ import annotations

import logging
import sys

import structlog
from azure.storage.blob import BlobServiceClient

from origination_common.config import load_common_settings
from origination_common.onelake import OneLakeWriter, select_credential
from origination_common.promoter import (
    SCAN_VERDICT_CLEAN,
    SCAN_VERDICT_MALICIOUS,
    _read_scan_verdict,
)

from .paths import STATE_ROOT, annex_state_path
from .state import LinkedDocument, read_state, write_state

log = structlog.get_logger()

_ANNEX_MARKER = "/annexes/"


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


def _is_annex_blob(path: str) -> bool:
    # Annex file blobs only; the state JSONL is written direct to OneLake, never staged.
    return _ANNEX_MARKER in path and f"{STATE_ROOT}/" not in path


def main() -> int:
    _configure_logging()
    settings = load_common_settings()
    if not settings.stg_account_name or not settings.fabric_workspace_name:
        log.error("annex_promoter_misconfigured", stg=bool(settings.stg_account_name), fabric=bool(settings.fabric_workspace_name))
        return 1

    cred = select_credential(settings.azure_client_id)
    stg = BlobServiceClient(account_url=f"https://{settings.stg_account_name}.blob.core.windows.net", credential=cred)
    untrusted = stg.get_container_client(settings.stg_container_untrusted)
    quarantine = stg.get_container_client(settings.stg_container_quarantine)
    onelake = OneLakeWriter(
        workspace_name=settings.fabric_workspace_name,
        lakehouse_name=settings.fabric_lakehouse_name,
        azure_client_id=settings.azure_client_id,
    )

    log.info("annex_promoter_run_start", account=settings.stg_account_name)
    promoted = quarantined = pending = scan_failed = 0
    state_cache: dict[str, dict] = {}  # state_path -> {key: LinkedDocument}, loaded lazily

    def _state_for(published_at: str) -> tuple[str, dict] | None:
        if not published_at:
            return None
        try:
            from datetime import date
            d = date.fromisoformat(published_at)
        except ValueError:
            return None
        sp = annex_state_path(d)
        if sp not in state_cache:
            state_cache[sp] = read_state(onelake, sp)
        return sp, state_cache[sp]

    def _update(meta: dict, status: str, **fields) -> None:
        st = _state_for(meta.get("published_at", ""))
        if st is None:
            return
        _sp, records = st
        key = (meta.get("announcement_external_id", ""), meta.get("sending_uuid", ""), meta.get("file_identifier", ""))
        existing = records.get(key)
        if existing is not None:
            records[key] = existing.model_copy(update={"status": status, **fields})
        else:  # defensive: rebuild a minimal record from blob metadata
            records[key] = LinkedDocument(
                announcement_external_id=key[0], url=meta.get("url", ""), host="almacen",
                sending_uuid=key[1], file_identifier=key[2], file_name=meta.get("file_name", ""),
                status=status, discovered_at=fields.get("fetched_at", ""), **fields,  # type: ignore[arg-type]
            )

    for blob in untrusted.list_blobs(include=["metadata"]):
        path = blob.name
        if not _is_annex_blob(path):
            continue
        bc = untrusted.get_blob_client(path)
        verdict = _read_scan_verdict(bc)
        if verdict is None:
            pending += 1
            log.info("annex_scan_pending", path=path)
            continue
        meta = bc.get_blob_properties().metadata or {}
        if verdict == SCAN_VERDICT_MALICIOUS:
            quarantine.get_blob_client(path).start_copy_from_url(bc.url)
            bc.delete_blob()
            _update(meta, "quarantined", error="malicious")
            quarantined += 1
            log.warning("annex_scan_malicious", path=path)
            continue
        if verdict != SCAN_VERDICT_CLEAN:
            scan_failed += 1
            _update(meta, "scan_failed", error=f"verdict:{verdict}")
            log.warning("annex_scan_failed", path=path, verdict=verdict)  # leave in staging; alert
            continue
        # Clean → stream to OneLake at the same path, then delete from staging.
        onelake.stream_from_blob(path, bc)
        bc.delete_blob()
        _update(meta, "promoted", file_path=path)
        promoted += 1
        log.info("annex_promoted", path=path)

    for sp, records in state_cache.items():
        write_state(onelake, sp, records)

    log.info("annex_promoter_run_done", promoted=promoted, quarantined=quarantined, pending=pending, scan_failed=scan_failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
