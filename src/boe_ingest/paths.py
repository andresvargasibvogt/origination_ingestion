"""Lakehouse-relative path generation.

All paths produced here are RELATIVE to the lakehouse's `Files/` root. The
writer (`onelake.py` or `LocalWriter`) prepends the workspace / lakehouse
prefix when actually writing. This keeps the manifest portable across
workspaces — the manifest's `pdf_path` field is the value these functions
return.

Source-parametric: every function accepts a `source` kwarg defaulting to
BOE so existing BOE callers don't change. BOA callers pass `source=SOURCE_BOA`.

See ADR-005 for the medallion layout contract.
"""

from __future__ import annotations

from datetime import date

from .manifest import SOURCE_BOE, Source


def bronze_root(source: Source = SOURCE_BOE) -> str:
    return f"bronze/{source}/raw"


def partition_dir(target_date: date, source: Source = SOURCE_BOE) -> str:
    """Hive-style partition path under the source's raw/ root."""
    return (
        f"{bronze_root(source)}/"
        f"year={target_date.year:04d}/"
        f"month={target_date.month:02d}/"
        f"day={target_date.day:02d}"
    )


def manifests_partition_dir(target_date: date, source: Source = SOURCE_BOE) -> str:
    """Hive-style partition path under _manifests/."""
    return (
        f"{bronze_root(source)}/_manifests/"
        f"year={target_date.year:04d}/"
        f"month={target_date.month:02d}/"
        f"day={target_date.day:02d}"
    )


def pdf_path(target_date: date, identifier: str, source: Source = SOURCE_BOE) -> str:
    return f"{partition_dir(target_date, source)}/{identifier}.pdf"


def manifest_path(target_date: date, source: Source = SOURCE_BOE) -> str:
    return f"{manifests_partition_dir(target_date, source)}/_manifest.json"
