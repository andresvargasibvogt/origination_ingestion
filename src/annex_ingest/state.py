"""The `linked_document` state — a JSONL state-manifest in OneLake Files/.

Per user decision, state lives as JSONL (one JSON object per line) under
`bronze/boe/annexes/_state/…`, not in a relational DB; the relational
`linked_document` table is built later by the silver layer from these files.

One record per (announcement, sending, file) citation so provenance is
preserved (the same annex can be cited by several announcements). The upsert is
keyed by that triple and is last-write-wins, mirroring the promoter's
merge-by-identifier (see origination_common/promoter.py). Files are tiny
(one line per linked file, most days zero), so whole-file read-modify-write is
fine — OneLake/ADLS has no append-in-place primitive anyway.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from origination_common.manifest import now_iso

# Lifecycle:
#   pending     — discovered, not yet fetched
#   fetched     — bytes landed in staging (link consumed; the deadline-bound step)
#   promoted    — clean-scanned + copied to OneLake (terminal-good)
#   skipped     — already in OneLake / duplicate sending / too large
#   expired     — the portal link was dead before we could fetch it
#   error       — transient/terminal fetch error (eligible for retry)
#   quarantined — Defender flagged it Malicious
#   scan_failed — Defender could not scan it (e.g. timeout); unverified, not promoted
Status = Literal[
    "pending", "fetched", "promoted", "skipped", "expired", "error", "quarantined", "scan_failed"
]

Host = Literal["almacen", "ssweb"]


class LinkedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    announcement_external_id: str          # e.g. BOE-B-2026-21237
    url: str                               # browse URL https://almacen.redsara.es/sending/public/<uuid>
    host: Host                             # "almacen" (v1); "ssweb" reserved for the legacy host
    sending_uuid: str
    file_identifier: str                   # almacen file uuid
    file_name: str
    status: Status
    file_path: str | None = None           # lakehouse-relative target (annex_file_path)
    content_type: str | None = None        # from the listing's mime
    bytes: int | None = None               # streamed byte count
    content_hash: str | None = None        # sha256 hex, computed while streaming
    discovered_at: str
    fetched_at: str | None = None
    expiration_date: str | None = None     # from the listing (ISO)
    error: str | None = None


Key = tuple[str, str, str]  # (announcement_external_id, sending_uuid, file_identifier)


def key_of(rec: LinkedDocument) -> Key:
    return (rec.announcement_external_id, rec.sending_uuid, rec.file_identifier)


class _TextIO(Protocol):
    """Minimal read/write surface satisfied by OneLakeWriter and LocalWriter."""

    def read_text(self, lakehouse_path: str) -> str | None: ...
    def write_text(self, lakehouse_path: str, text: str, metadata: dict[str, str] | None = ...) -> None: ...


def read_state(io: _TextIO, path: str) -> dict[Key, LinkedDocument]:
    """Load the JSONL state-manifest into a dict keyed by (announcement, sending, file).

    Returns {} when the file doesn't exist yet (first run for the day).
    """
    text = io.read_text(path)
    if not text:
        return {}
    out: dict[Key, LinkedDocument] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rec = LinkedDocument.model_validate_json(line)
        out[key_of(rec)] = rec
    return out


def write_state(io: _TextIO, path: str, records: dict[Key, LinkedDocument]) -> None:
    """Serialize records back to JSONL (stable order for clean diffs)."""
    lines = [
        records[k].model_dump_json()
        for k in sorted(records, key=lambda t: (t[0], t[1], t[2]))
    ]
    io.write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def upsert(records: dict[Key, LinkedDocument], rec: LinkedDocument) -> None:
    """Last-write-wins merge by (announcement, sending, file)."""
    records[key_of(rec)] = rec


def now() -> str:
    return now_iso()
