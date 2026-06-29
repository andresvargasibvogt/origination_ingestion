"""Lakehouse-relative path helpers for the annex tier.

The shared `origination_common.paths` helpers are Hive year/month/day
partitioned, which doesn't fit annexes (keyed by announcement + sending + file,
not by date). These are kept package-local so the shared `Granularity`/`Source`
contract isn't widened.

Layout (relative to `Files/`):

    bronze/boe/annexes/<announcement_id>/<sending_uuid>/<file_identifier>__<name>
    bronze/boe/annexes/_state/year=YYYY/month=MM/day=DD/_linked_documents.jsonl

`<announcement_id>` (e.g. BOE-B-2026-21237) and `<sending_uuid>` are ASCII-safe
by construction (ADR-005 rule: identifiers as path segments, never free text).
The filename is prefixed with the file's own uuid so two files sharing a name
within one sending never collide; the human name is ASCII-folded for the path
and preserved verbatim in the state/metadata.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

# Annexes ride the existing "boe" source segment so they sit under the same
# bronze/boe/ tree as the announcements that reference them.
ANNEX_SOURCE = "boe"
ANNEX_ROOT = f"bronze/{ANNEX_SOURCE}/annexes"
STATE_ROOT = f"{ANNEX_ROOT}/_state"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _ascii_fold(s: str) -> str:
    """Strip diacritics → plain ASCII (mirror blob._ascii_fold)."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def safe_name(file_identifier: str, file_name: str) -> str:
    """`<file_identifier>__<ascii-folded, path-safe name>`.

    Guarantees uniqueness within a sending (uuid prefix) and an ASCII-safe path
    segment, while keeping the human name readable.
    """
    folded = _ascii_fold(file_name).strip()
    cleaned = _UNSAFE.sub("_", folded).strip("_") or "file"
    return f"{file_identifier}__{cleaned}"


def annex_dir(announcement_id: str, sending_uuid: str) -> str:
    return f"{ANNEX_ROOT}/{announcement_id}/{sending_uuid}"


def annex_file_path(announcement_id: str, sending_uuid: str, file_identifier: str, file_name: str) -> str:
    return f"{annex_dir(announcement_id, sending_uuid)}/{safe_name(file_identifier, file_name)}"


def annex_state_path(target_date: date) -> str:
    """JSONL state-manifest path, partitioned by the announcement publication day."""
    suffix = f"year={target_date.year:04d}/month={target_date.month:02d}/day={target_date.day:02d}"
    return f"{STATE_ROOT}/{suffix}/_linked_documents.jsonl"
