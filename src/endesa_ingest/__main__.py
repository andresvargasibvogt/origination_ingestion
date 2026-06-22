"""CLI entry point for the e-distribución poller.

    python -m endesa_ingest
        # Cloud: discover latest CSV, dedup against OneLake, land in staging
        # if new. Used by the daily-poll caj-endesa-monthly Job.

    python -m endesa_ingest --out-dir ./out
        # Local: download the latest CSV to ./out (no dedup, no Azure creds).

    python -m endesa_ingest --force
        # Re-download + re-land even if the version is already in OneLake.

Exit codes:
    0  success (including "nothing new" no-op)
    1  configuration error
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import structlog

from origination_common.blob import BlobWriter
from origination_common.onelake import LocalWriter, OneLakeWriter, Writer

from .config import Settings, load_settings
from .orchestrator import ingest_latest


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


def _build_writer_and_reader(
    args: argparse.Namespace, settings: Settings
) -> tuple[Writer, OneLakeWriter | None, bool]:
    """Return (writer, onelake_reader_for_dedup, emit_manifest_via_writer)."""
    # Local mode: write to disk, no dedup, manifest written locally.
    if args.out_dir is not None:
        return LocalWriter(out_dir=Path(args.out_dir)), None, True

    # Staging mode (default in cloud): write to the Defender-scanned staging
    # container; the promoter writes the OneLake manifest. Dedup reads OneLake.
    if settings.stg_account_name:
        writer = BlobWriter(
            account_name=settings.stg_account_name,
            container=settings.stg_container_untrusted,
            azure_client_id=settings.azure_client_id,
        )
        reader: OneLakeWriter | None = None
        if settings.fabric_workspace_name:
            reader = OneLakeWriter(
                workspace_name=settings.fabric_workspace_name,
                lakehouse_name=settings.fabric_lakehouse_name,
                azure_client_id=settings.azure_client_id,
            )
        return writer, reader, False

    # Direct-to-OneLake escape hatch — writer doubles as the dedup reader.
    if settings.fabric_workspace_name:
        writer = OneLakeWriter(
            workspace_name=settings.fabric_workspace_name,
            lakehouse_name=settings.fabric_lakehouse_name,
            azure_client_id=settings.azure_client_id,
        )
        return writer, writer, True

    raise SystemExit(
        "No writer configured. Set --out-dir for local, "
        "STG_ACCOUNT_NAME for staging (Pattern F), "
        "or FABRIC_WORKSPACE_NAME for direct-to-OneLake."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="endesa-ingest", description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=str,
        help="Write to local directory instead of OneLake (no dedup)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the version is already present in OneLake",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    settings = load_settings()
    writer, reader, emit_manifest = _build_writer_and_reader(args, settings)

    asyncio.run(
        ingest_latest(
            writer,
            settings,
            onelake_reader=reader,
            emit_manifest=emit_manifest,
            force=args.force,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
