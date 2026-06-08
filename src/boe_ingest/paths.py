"""Lakehouse-relative path generation.

All paths produced here are RELATIVE to the lakehouse's `Files/` root. The
writer (`onelake.py` or `LocalWriter`) prepends the workspace / lakehouse
prefix when actually writing. This keeps the manifest portable across
workspaces — the manifest's `pdf_path` field is the value these functions
return.

See ADR-005 for the contract.
"""

from __future__ import annotations

from datetime import date

from .config import BRONZE_ROOT


def partition_dir(target_date: date) -> str:
    """Hive-style partition path under the source's raw/ root."""
    return (
        f"{BRONZE_ROOT}/"
        f"year={target_date.year:04d}/"
        f"month={target_date.month:02d}/"
        f"day={target_date.day:02d}"
    )


def manifests_partition_dir(target_date: date) -> str:
    """Hive-style partition path under _manifests/."""
    return (
        f"{BRONZE_ROOT}/_manifests/"
        f"year={target_date.year:04d}/"
        f"month={target_date.month:02d}/"
        f"day={target_date.day:02d}"
    )


def pdf_path(target_date: date, identifier: str) -> str:
    return f"{partition_dir(target_date)}/{identifier}.pdf"


def manifest_path(target_date: date) -> str:
    return f"{manifests_partition_dir(target_date)}/_manifest.json"
