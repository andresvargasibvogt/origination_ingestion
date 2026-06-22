"""REE poll-and-land orchestration.

One run:
  1. fetch the landing page → discover the latest capacity CSV + its pub date
  2. dedup: if that version already landed in OneLake, no-op (clean log, exit)
  3. otherwise download the CSV → write to staging with per-blob metadata
  4. build a 1-item manifest (the promoter writes it to OneLake after Defender
     clears the file, same as BOE/BOA)

The unit of work is "the latest published file", not "today's file" — REE
publishes monthly on an uncertain day, so the cron polls daily and this
orchestrator is a no-op until a genuinely new version appears.
"""

from __future__ import annotations

import httpx
import structlog

from origination_common.fetcher import PDFFetcher, PDFFetchError
from origination_common.manifest import (
    ATTRIBUTION_REE,
    SOURCE_REE,
    FailedItem,
    ItemEntry,
    Manifest,
    RunInfo,
    now_iso,
    sha256_hex,
)
from origination_common.onelake import OneLakeWriter, Writer
from origination_common.paths import manifest_path, partition_dir
from origination_common.robots import RobotsGuard

from .config import REE_BASE_URL, Settings
from .discover import fetch_landing_page, find_latest_csv

log = structlog.get_logger()

# REE reports the issuing org rather than a gazette section/departamento.
REE_DEPARTAMENTO = "Red Eléctrica de España"


async def ingest_latest(
    writer: Writer,
    settings: Settings,
    *,
    onelake_reader: OneLakeWriter | None = None,
    emit_manifest: bool = True,
    force: bool = False,
) -> Manifest:
    """Discover + land the latest REE capacity CSV. Idempotent (deduped).

    `onelake_reader`, when supplied, is used for the dedup existence check:
    if the target version already exists in OneLake, we skip the download
    and return an unchanged (zero-written) manifest. `force=True` bypasses
    the check (re-download + re-land).
    """
    started = now_iso()
    log.info("ree_run_start")

    robots = RobotsGuard(base_url=REE_BASE_URL, user_agent=settings.user_agent)
    robots.load()

    async with httpx.AsyncClient(
        base_url=REE_BASE_URL,
        headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout_secs,
        http2=True,
        follow_redirects=True,
    ) as client:
        # 1. Discover
        html = await fetch_landing_page(client)
        latest = find_latest_csv(html)

        # REE is monthly → month-level partition (no day= folder).
        target_rel_path = (
            f"{partition_dir(latest.published_at, source=SOURCE_REE, granularity='month')}"
            f"/{latest.filename}"
        )
        identifier = latest.filename.removesuffix(".csv")
        full_url = f"{REE_BASE_URL}{latest.url_path}"

        # 2. Dedup — has this version already landed in OneLake?
        if not force and onelake_reader is not None and onelake_reader.exists(target_rel_path):
            log.info(
                "ree_version_already_present",
                filename=latest.filename,
                published_at=latest.published_at.isoformat(),
                path=target_rel_path,
            )
            return _unchanged_manifest(latest.published_at.isoformat(), started)

        # 3. Robots + download
        items_failed: list[FailedItem] = []
        items_written: list[ItemEntry] = []
        items_robots_blocked = 0

        if not robots.can_fetch(full_url):
            items_robots_blocked = 1
            log.info("robots_blocked", identifier=identifier, url=full_url)
        else:
            fetcher = PDFFetcher(client=client, concurrency=1, throttle_secs=0.0)
            try:
                data = await fetcher.fetch(latest.url_path)
            except PDFFetchError as exc:
                items_failed.append(FailedItem(identifier=identifier, reason=str(exc)))
                data = None

            if data is not None:
                sha = sha256_hex(data)
                blob_metadata: dict[str, str] = {
                    "identifier": identifier,
                    "section": "",
                    "departamento_codigo": "",
                    "departamento": REE_DEPARTAMENTO,
                    "published_at": latest.published_at.isoformat(),
                    "sha256": sha,
                    "size_bytes": str(len(data)),
                    "url_pdf": full_url,
                }
                writer.write_bytes(target_rel_path, data, metadata=blob_metadata)
                items_written.append(
                    ItemEntry(
                        identifier=identifier,
                        section="",
                        subsection=None,
                        departamento_codigo="",
                        departamento=REE_DEPARTAMENTO,
                        published_at=latest.published_at.isoformat(),
                        url_pdf=full_url,
                        url_xml=None,
                        url_html=None,
                        eli=None,
                        pdf_path=target_rel_path,
                        sha256=sha,
                        size_bytes=len(data),
                    )
                )

    # 4. Manifest
    run = RunInfo(
        started_at=started,
        ended_at=now_iso(),
        date=latest.published_at.isoformat(),
        sumario_items_total=1,
        items_filtered_in=1,
        items_written=len(items_written),
        items_failed=items_failed,
        items_robots_blocked=items_robots_blocked,
        attribution=ATTRIBUTION_REE,
    )
    manifest = Manifest(source=SOURCE_REE, run=run, items=items_written)
    if emit_manifest:
        writer.write_text(
            manifest_path(latest.published_at, source=SOURCE_REE, granularity="month"),
            manifest.to_json(),
        )
    log.info(
        "ree_run_done",
        published_at=latest.published_at.isoformat(),
        written=run.items_written,
        failed=len(run.items_failed),
        robots_blocked=run.items_robots_blocked,
        manifest_emitted=emit_manifest,
    )
    return manifest


def _unchanged_manifest(published_at_iso: str, started: str) -> Manifest:
    """Zero-written manifest for a poll that found nothing new."""
    run = RunInfo(
        started_at=started,
        ended_at=now_iso(),
        date=published_at_iso,
        sumario_items_total=1,
        items_filtered_in=0,
        items_written=0,
        attribution=ATTRIBUTION_REE,
    )
    return Manifest(source=SOURCE_REE, run=run, items=[])
