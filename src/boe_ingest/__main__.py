"""CLI entry point.

Two modes:

    python -m boe_ingest --date 2026-05-27
        # Cloud: writes to OneLake under FABRIC_WORKSPACE_NAME / FABRIC_LAKEHOUSE_NAME.

    python -m boe_ingest --date 2026-05-27 --out-dir ./out
        # Local: writes to ./out/ instead of OneLake. No Azure creds required.

    python -m boe_ingest --date today
        # Resolves 'today' in Europe/Madrid (BOE's publication timezone).

    python -m boe_ingest --from 2026-05-20 --to 2026-05-27
        # Backfill: same code path, looped over the date range.

Exit codes:
    0  success (may include an empty-day manifest)
    1  configuration error (missing env var, bad args)
    2  runtime error (sumario fetch failed unrecoverably, etc.)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from .blob import BlobWriter
from .config import Settings, load_settings
from .onelake import LocalWriter, OneLakeWriter, Writer
from .orchestrator import ingest_one_day
from .relevance import RelevanceConfig

MADRID_TZ = ZoneInfo("Europe/Madrid")


def _parse_date(raw: str) -> date:
    if raw == "today":
        return datetime.now(MADRID_TZ).date()
    return date.fromisoformat(raw)


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"--to ({end}) must be >= --from ({start})")
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


def _build_writer(args: argparse.Namespace, settings: Settings) -> tuple[Writer, bool]:
    """Return (writer, emit_manifest_via_writer)."""
    # Local mode: --out-dir → LocalWriter, manifest written locally.
    if args.out_dir is not None:
        return LocalWriter(out_dir=Path(args.out_dir)), True

    # Staging mode (default in cloud): write to the staging Blob container.
    # The promoter writes the OneLake manifest after scan results arrive.
    if settings.stg_account_name:
        writer = BlobWriter(
            account_name=settings.stg_account_name,
            container=settings.stg_container_untrusted,
            azure_client_id=settings.azure_client_id,
        )
        return writer, False

    # Direct-to-OneLake escape hatch (for local dev or if Pattern F is rolled
    # back per ADR-008). Only used when STG_ACCOUNT_NAME is unset AND
    # FABRIC_WORKSPACE_NAME is set.
    if settings.fabric_workspace_name:
        writer = OneLakeWriter(
            workspace_name=settings.fabric_workspace_name,
            lakehouse_name=settings.fabric_lakehouse_name,
            azure_client_id=settings.azure_client_id,
        )
        return writer, True

    raise SystemExit(
        "No writer configured. Set --out-dir for local, "
        "STG_ACCOUNT_NAME for staging (Pattern F), "
        "or FABRIC_WORKSPACE_NAME for direct-to-OneLake."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boe-ingest", description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", type=str, help="ISO date (YYYY-MM-DD) or 'today'")
    group.add_argument(
        "--from",
        dest="date_from",
        type=_parse_date,
        help="Backfill start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        type=_parse_date,
        help="Backfill end date (YYYY-MM-DD); required with --from",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        help="Write to local directory instead of OneLake",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


async def _run(
    dates: list[date],
    writer: Writer,
    relevance: RelevanceConfig,
    settings: Settings,
    emit_manifest: bool,
) -> int:
    for d in dates:
        await ingest_one_day(
            d,
            writer=writer,
            relevance=relevance,
            settings=settings,
            emit_manifest=emit_manifest,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.date_from is not None:
        if args.date_to is None:
            parser.error("--from requires --to")
        dates = _date_range(args.date_from, args.date_to)
    else:
        dates = [_parse_date(args.date)]

    settings = load_settings()
    relevance = RelevanceConfig.load(settings.relevance_config_path)
    writer, emit_manifest = _build_writer(args, settings)

    return asyncio.run(
        _run(
            dates,
            writer=writer,
            relevance=relevance,
            settings=settings,
            emit_manifest=emit_manifest,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
