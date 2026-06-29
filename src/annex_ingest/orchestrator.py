"""End-to-end annex acquisition flow.

For each discovered announcement: list each almacen sending, and for each file
either skip (already landed / too large) or stream-download it to a local temp
file (bounded memory) and hand it to the writer's `put_file` (staging blob in
cloud, local dir in --out-dir, OneLake in the direct escape hatch). Outcomes are
recorded in the JSONL state-manifest, written incrementally per sending so a
long backfill that crashes keeps its progress.

The FETCH (to staging) is the deadline-bound step — it consumes the expiring
portal link. Promotion to OneLake (clean blobs out of the Defender DMZ) is a
separate, lag-tolerant step handled by the annex-promoter (promoter.py).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import structlog
from pydantic import BaseModel

from .almacen import (
    AlmacenClient,
    AlmacenError,
    AlmacenNetworkError,
    AlmacenNotFound,
)
from .config import ALMACEN_BASE_URL, Settings
from .discover import AnnouncementWork
from .paths import annex_file_path, annex_state_path
from .state import LinkedDocument, now, read_state, upsert, write_state

log = structlog.get_logger()


def _browse_url(uuid: str) -> str:
    return f"{ALMACEN_BASE_URL}/sending/public/{uuid}"


class RunSummary(BaseModel):
    announcements: int = 0
    filtered: int = 0          # announcements skipped by the project-type/MW gate
    sendings: int = 0
    fetched: int = 0
    skipped: int = 0
    expired: int = 0
    errors: int = 0


async def acquire(
    works: list[AnnouncementWork],
    client_obj: AlmacenClient,
    *,
    writer,                 # put_file(path, local_fspath, metadata, content_type)
    state_io,               # read_text / write_text (OneLakeWriter or LocalWriter)
    onelake_reader,         # exists() for dedup, or None (local mode)
    settings: Settings,
    force: bool = False,
) -> RunSummary:
    summary = RunSummary()
    # Group by the publication day so each day's state-manifest is one file.
    by_day: dict[str, list[AnnouncementWork]] = {}
    for w in works:
        by_day.setdefault(w.published_at.isoformat(), []).append(w)

    with tempfile.TemporaryDirectory(prefix="annex-") as tmp:
        tmpdir = Path(tmp)
        for day_iso, day_works in by_day.items():
            state_path = annex_state_path(day_works[0].published_at)
            state = read_state(state_io, state_path)
            for work in day_works:
                summary.announcements += 1
                ann = work.announcement_external_id
                if not work.fetch_annexes:
                    # Project-type/MW gate said no — record why, fetch nothing.
                    summary.filtered += 1
                    for uuid in work.sending_uuids:
                        _record(state, ann, _browse_url(uuid), uuid, "", "", "skipped",
                                summary, error=f"gate:{work.gate_reason}")
                        summary.skipped += 1
                    if work.sending_uuids:
                        write_state(state_io, state_path, state)
                    continue
                if not work.sending_uuids:
                    # In-scope, but no almacen link (portal office-only/unresolved) — record for audit.
                    if work.portal_status in ("office_only", "unresolved"):
                        _record(state, ann, work.project_url or "", "", "", "", "skipped",
                                summary, error=f"portal_{work.portal_status}")
                        summary.skipped += 1
                        write_state(state_io, state_path, state)
                    continue
                for uuid in work.sending_uuids:
                    summary.sendings += 1
                    await _process_sending(
                        ann, uuid, client_obj, writer=writer, onelake_reader=onelake_reader,
                        settings=settings, force=force, state=state, summary=summary, tmpdir=tmpdir,
                        published_at=day_iso,
                    )
                    # Incremental durability: persist after each sending.
                    write_state(state_io, state_path, state)
            write_state(state_io, state_path, state)
    log.info(
        "annex_run_done",
        announcements=summary.announcements, filtered=summary.filtered, sendings=summary.sendings,
        fetched=summary.fetched, skipped=summary.skipped, expired=summary.expired, errors=summary.errors,
    )
    return summary


async def _process_sending(
    ann: str, uuid: str, client_obj: AlmacenClient, *,
    writer, onelake_reader, settings: Settings, force: bool, state: dict, summary: RunSummary, tmpdir: Path,
    published_at: str,
) -> None:
    browse = _browse_url(uuid)
    try:
        listing = await client_obj.list_sending(uuid)
    except AlmacenNotFound:
        _record(state, ann, browse, uuid, "", "", "expired", summary, error="sending_not_found")
        summary.expired += 1
        return
    except (AlmacenNetworkError, AlmacenError) as exc:
        _record(state, ann, browse, uuid, "", "", "error", summary, error=str(exc))
        summary.errors += 1
        return

    expired = listing.is_expired()
    for f in listing.files:
        key = (ann, uuid, f.identifier)
        target = annex_file_path(ann, uuid, f.identifier, f.name)
        existing = state.get(key)
        if not force and existing and existing.status in ("fetched", "promoted", "skipped"):
            continue  # resume: already handled in a prior run
        if expired:
            _record(state, ann, browse, uuid, f.identifier, f.name, "expired", summary,
                     expiration_date=listing.expiration_date, content_type=f.mime)
            summary.expired += 1
            continue
        if not force and onelake_reader is not None and onelake_reader.exists(target):
            _record(state, ann, browse, uuid, f.identifier, f.name, "skipped", summary,
                    file_path=target, content_type=f.mime, expiration_date=listing.expiration_date,
                    error="already_in_onelake")
            summary.skipped += 1
            continue
        if settings.max_file_bytes and f.size > settings.max_file_bytes:
            _record(state, ann, browse, uuid, f.identifier, f.name, "skipped", summary,
                    content_type=f.mime, bytes=f.size, expiration_date=listing.expiration_date,
                    error=f"too_large:{f.size}")
            summary.skipped += 1
            continue

        dest = tmpdir / f"{uuid}.{f.identifier}"
        try:
            sha, nbytes = await client_obj.download_to_file(uuid, f.identifier, dest)
        except AlmacenNotFound:
            status = "expired" if expired else "error"
            _record(state, ann, browse, uuid, f.identifier, f.name, status, summary,
                    expiration_date=listing.expiration_date, error="file_not_found")
            summary.expired += 1 if status == "expired" else 0
            summary.errors += 1 if status == "error" else 0
            continue
        except (AlmacenNetworkError, AlmacenError) as exc:
            _record(state, ann, browse, uuid, f.identifier, f.name, "error", summary,
                    expiration_date=listing.expiration_date, error=str(exc))
            summary.errors += 1
            continue

        metadata = {
            "announcement_external_id": ann,
            "sending_uuid": uuid,
            "file_identifier": f.identifier,
            "file_name": f.name,
            "sha256": sha,
            "size_bytes": str(nbytes),
            "content_type": f.mime,
            "published_at": published_at,
            "url": browse,
            "source": "boe",
        }
        try:
            writer.put_file(target, dest, metadata=metadata, content_type=f.mime)
        finally:
            dest.unlink(missing_ok=True)
        _record(state, ann, browse, uuid, f.identifier, f.name, "fetched", summary,
                file_path=target, content_type=f.mime, bytes=nbytes, content_hash=sha,
                fetched_at=now(), expiration_date=listing.expiration_date)
        summary.fetched += 1


def _record(state: dict, ann: str, url: str, uuid: str, file_id: str, file_name: str, status: str,
            summary: RunSummary, **fields) -> None:
    rec = LinkedDocument(
        announcement_external_id=ann, url=url, host="almacen", sending_uuid=uuid,
        file_identifier=file_id, file_name=file_name, status=status,  # type: ignore[arg-type]
        discovered_at=now(), **fields,
    )
    upsert(state, rec)
