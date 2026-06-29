"""CLI entry point for the annex acquisition tier.

    python -m annex_ingest --date today
        # Read the day's promoted BOE manifest, discover annex links, fetch to
        # staging (Defender DMZ). Used by the daily caj-annex-daily Job.

    python -m annex_ingest --backfill 2026-04-01:2026-06-22
        # Best-effort backfill over a date range (links older than ~3 months
        # are likely expired and recorded as such).

    python -m annex_ingest --announcement BOE-B-2026-21237 --out-dir ./out
        # Ad-hoc/local: fetch one announcement's annexes to a local dir (no
        # OneLake, no dedup). Used for dry-runs.

    --force re-fetches even if already landed; -v is verbose.

Exit codes: 0 success (incl. nothing-to-do), 1 config error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import structlog

from origination_common.blob import BlobWriter
from origination_common.onelake import LocalWriter, OneLakeWriter

from .almacen import AlmacenClient
from .config import ALMACEN_BASE_URL, BOE_BASE_URL, Settings, load_settings
from .discover import AnnouncementWork, announcements_from_manifest, discover_one, xml_url_for
from .orchestrator import acquire

log = structlog.get_logger()
MADRID_TZ = ZoneInfo("Europe/Madrid")


def _parse_date(raw: str) -> date:
    if raw == "today":
        return datetime.now(MADRID_TZ).date()
    return date.fromisoformat(raw)


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"end ({end}) must be >= start ({start})")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


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


def _build_writer_reader_state(args: argparse.Namespace, settings: Settings):
    """Return (writer, onelake_reader_or_None, state_io).

    - local (--out-dir): LocalWriter for bytes + state; no OneLake dedup.
    - staging (default cloud): BlobWriter for bytes; OneLakeWriter for dedup + state.
    - direct (escape hatch): OneLakeWriter for everything.
    """
    if args.out_dir is not None:
        local = LocalWriter(out_dir=Path(args.out_dir))
        # Bytes + state stay local, but read the day's BOE manifest from OneLake
        # when Fabric creds are present, so --date/--backfill can run against the
        # real ingested files. (exists() dedup is harmless here — nothing local
        # is in OneLake.) No creds → --announcement-only local mode.
        reader = (
            OneLakeWriter(settings.fabric_workspace_name, settings.fabric_lakehouse_name, settings.azure_client_id)
            if settings.fabric_workspace_name else None
        )
        return local, reader, local
    if settings.stg_account_name:
        writer = BlobWriter(
            account_name=settings.stg_account_name,
            container=settings.stg_container_untrusted,
            azure_client_id=settings.azure_client_id,
        )
        onelake = (
            OneLakeWriter(settings.fabric_workspace_name, settings.fabric_lakehouse_name, settings.azure_client_id)
            if settings.fabric_workspace_name else None
        )
        return writer, onelake, onelake
    if settings.fabric_workspace_name:
        onelake = OneLakeWriter(settings.fabric_workspace_name, settings.fabric_lakehouse_name, settings.azure_client_id)
        return onelake, onelake, onelake
    raise SystemExit("No writer configured. Set --out-dir, STG_ACCOUNT_NAME, or FABRIC_WORKSPACE_NAME.")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="annex-ingest", description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--date", type=str, help="ISO date or 'today' — read that day's BOE manifest")
    g.add_argument("--backfill", metavar="FROM:TO", help="Backfill date range as one token, e.g. 2026-04-01:2026-06-22")
    g.add_argument("--announcement", nargs="+", help="One or more BOE announcement ids (ad-hoc/local)")
    p.add_argument("--out-dir", type=str, help="Write to a local directory instead of OneLake (no dedup)")
    p.add_argument("--force", action="store_true", help="Re-fetch even if already landed")
    p.add_argument("--no-filter", action="store_true",
                   help="Disable the project-type/MW gate (fetch every announcement's annexes)")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p


def _enumerate_pairs(args: argparse.Namespace, reader) -> list[tuple[str, str, date]]:
    """(announcement_id, url_xml, fallback_publication_date)."""
    if args.announcement:
        today = datetime.now(MADRID_TZ).date()
        return [(a, xml_url_for(a, BOE_BASE_URL), today) for a in args.announcement]
    if args.backfill:
        try:
            from_s, to_s = args.backfill.split(":")
        except ValueError:
            raise SystemExit("--backfill expects FROM:TO, e.g. 2026-04-01:2026-06-22") from None
        dates = _date_range(_parse_date(from_s), _parse_date(to_s))
    else:
        dates = [_parse_date(args.date or "today")]
    if reader is None:
        raise SystemExit("--date/--backfill need OneLake (set STG_ACCOUNT_NAME or FABRIC_WORKSPACE_NAME); use --announcement for local runs.")
    pairs: list[tuple[str, str, date]] = []
    for d in dates:
        for ident, url_xml in announcements_from_manifest(reader, d):
            pairs.append((ident, url_xml, d))
    return pairs


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    writer, onelake_reader, state_io = _build_writer_reader_state(args, settings)
    pairs = _enumerate_pairs(args, onelake_reader if onelake_reader is not None else state_io)

    works: list[AnnouncementWork] = []
    async with httpx.AsyncClient(
        base_url=BOE_BASE_URL, headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout_secs, http2=True, follow_redirects=True,
    ) as boe_client:
        apply_filter = settings.apply_project_filter and not args.no_filter
        for ident, url_xml, fallback in pairs:
            try:
                work = await discover_one(
                    boe_client, ident, url_xml, fallback_date=fallback,
                    apply_filter=apply_filter, min_mw=settings.min_mw,
                )
            except httpx.HTTPError as exc:
                log.warning("annex_discover_failed", announcement=ident, error=str(exc))
                continue
            if work.sending_uuids:
                works.append(work)
    log.info("annex_discovery_summary", announcements_seen=len(pairs), with_links=len(works))
    if not works:
        return 0

    async with httpx.AsyncClient(
        base_url=ALMACEN_BASE_URL, headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout_secs, http2=True, follow_redirects=True,
    ) as almacen_client:
        client_obj = AlmacenClient(
            almacen_client, concurrency=settings.concurrency,
            throttle_secs=settings.throttle_secs, chunk_bytes=settings.chunk_bytes,
        )
        await acquire(
            works, client_obj, writer=writer, state_io=state_io,
            onelake_reader=onelake_reader, settings=settings, force=args.force,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    settings = load_settings()
    return asyncio.run(_run(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
