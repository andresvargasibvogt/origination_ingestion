"""End-to-end orchestration of one daily BOA run.

Mirrors `boe_ingest.orchestrator`: fetch the day's sumario, walk items,
apply the relevance filter, fetch each PDF, write to staging with per-item
metadata. The promoter (shared across sources) upserts the OneLake manifest
after Defender's scan tags arrive.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx
import structlog

# Reused infrastructure from the shared package — source-agnostic by design.
from origination_common.fetcher import PDFFetchError, PDFFetcher
from origination_common.manifest import (
    ATTRIBUTION_BOA,
    SOURCE_BOA,
    FailedItem,
    ItemEntry,
    Manifest,
    RunInfo,
    now_iso,
    sha256_hex,
)
from origination_common.onelake import Writer
from origination_common.paths import manifest_path, pdf_path
from origination_common.robots import RobotsGuard

from .config import BOA_BASE_URL, Settings
from .relevance import RelevanceConfig, passes_filter
from .sumario import EmptyDay, extract_mlkob, extract_pdf_url, fetch_sumario

log = structlog.get_logger()


def _yyyymmdd(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}{d.day:02d}"


def _extract_section_code(seccion_full: str) -> str:
    """`"V. Anuncios"` → `"V"`. Empty for malformed values."""
    return seccion_full.split(".", 1)[0].strip()


def _extract_subsection_code(subseccion_full: str) -> str | None:
    """`"b) Otros anuncios"` → `"b"`. None for empty BOA fields."""
    if not subseccion_full:
        return None
    code = subseccion_full.split(")", 1)[0].strip()
    return code or None


async def ingest_one_day(
    target_date: date,
    writer: Writer,
    relevance: RelevanceConfig,
    settings: Settings,
    emit_manifest: bool = True,
) -> Manifest:
    """Run a single day's BOA ingestion. Always returns a Manifest.

    When `emit_manifest=False` (staging mode), the Manifest is built and
    returned but NOT written via the writer — the promoter builds the
    OneLake manifest after scan results arrive.
    """
    started = now_iso()
    log.info("boa_run_start", date=target_date.isoformat())

    robots = RobotsGuard(base_url=BOA_BASE_URL, user_agent=settings.user_agent)
    robots.load()

    async with httpx.AsyncClient(
        base_url=BOA_BASE_URL,
        headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout_secs,
        http2=True,
        follow_redirects=True,
    ) as client:
        # 1. Sumario
        try:
            items_raw = await fetch_sumario(client, _yyyymmdd(target_date))
        except EmptyDay:
            log.info("boa_empty_day", date=target_date.isoformat())
            manifest = _empty_manifest(target_date, started)
            if emit_manifest:
                writer.write_text(
                    manifest_path(target_date, source=SOURCE_BOA),
                    manifest.to_json(),
                )
            return manifest

        sumario_count = len(items_raw)
        log.info("boa_sumario_items_total", count=sumario_count)

        # 2. Relevance filter — section + subsection + departamento.
        selected: list[dict[str, Any]] = []
        for it in items_raw:
            section_code = _extract_section_code(str(it.get("Seccion", "")))
            subsection_code = _extract_subsection_code(str(it.get("Subseccion", "")))
            emisor = str(it.get("Emisor", ""))
            if passes_filter(section_code, subsection_code, emisor, relevance):
                selected.append(it)
        log.info("boa_items_filtered_in", count=len(selected))

        # 3. Per-item PDF fetch
        fetcher = PDFFetcher(
            client=client,
            concurrency=settings.pdf_concurrency,
            throttle_secs=settings.pdf_throttle_secs,
        )

        items_written: list[ItemEntry] = []
        items_failed: list[FailedItem] = []
        items_robots_blocked = 0

        async def _process(it: dict[str, Any]) -> ItemEntry | None:
            nonlocal items_robots_blocked
            mlkob_or_docn = extract_mlkob(extract_pdf_url(it) or "") or str(it.get("DOCN", "")).strip()
            url_pdf = extract_pdf_url(it)
            if not mlkob_or_docn or not url_pdf:
                items_failed.append(
                    FailedItem(
                        identifier=mlkob_or_docn or it.get("DOCN", "?"),
                        reason="missing MLKOB or url_pdf",
                    )
                )
                return None
            if not robots.can_fetch(url_pdf):
                items_robots_blocked += 1
                log.info("robots_blocked", identifier=mlkob_or_docn, url=url_pdf)
                return None
            try:
                pdf_bytes = await fetcher.fetch(url_pdf)
            except PDFFetchError as exc:
                items_failed.append(FailedItem(identifier=mlkob_or_docn, reason=str(exc)))
                return None
            pdf_rel_path = pdf_path(target_date, mlkob_or_docn, source=SOURCE_BOA)
            sha = sha256_hex(pdf_bytes)
            section_code = _extract_section_code(str(it.get("Seccion", "")))
            subsection_code = _extract_subsection_code(str(it.get("Subseccion", "")))
            emisor = str(it.get("Emisor", ""))
            # Per-blob metadata — promoter reads this to build the OneLake manifest.
            # Length cap matches BOE's; Azure blob metadata limit is 8000 chars per value.
            blob_metadata: dict[str, str] = {
                "identifier": mlkob_or_docn,
                "section": section_code,
                "departamento_codigo": "",  # BOA doesn't expose a codigo in the JSON
                "departamento": emisor[:8000],
                "published_at": target_date.isoformat(),
                "sha256": sha,
                "size_bytes": str(len(pdf_bytes)),
                "url_pdf": url_pdf,
            }
            if subsection_code:
                blob_metadata["subsection"] = subsection_code
            writer.write_bytes(pdf_rel_path, pdf_bytes, metadata=blob_metadata)
            return ItemEntry(
                identifier=mlkob_or_docn,
                section=section_code,
                subsection=subsection_code,
                departamento_codigo="",
                departamento=emisor,
                published_at=target_date.isoformat(),
                url_pdf=url_pdf,
                url_xml=None,
                url_html=None,
                eli=None,
                pdf_path=pdf_rel_path,
                sha256=sha,
                size_bytes=len(pdf_bytes),
            )

        tasks = [asyncio.create_task(_process(it)) for it in selected]
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result is not None:
                items_written.append(result)

    # 4. Manifest
    run = RunInfo(
        started_at=started,
        ended_at=now_iso(),
        date=target_date.isoformat(),
        sumario_items_total=sumario_count,
        items_filtered_in=len(selected),
        items_written=len(items_written),
        items_failed=items_failed,
        items_robots_blocked=items_robots_blocked,
        attribution=ATTRIBUTION_BOA,
    )
    manifest = Manifest(source=SOURCE_BOA, run=run, items=items_written)
    if emit_manifest:
        writer.write_text(
            manifest_path(target_date, source=SOURCE_BOA),
            manifest.to_json(),
        )
    log.info(
        "boa_run_done",
        date=target_date.isoformat(),
        written=run.items_written,
        failed=len(run.items_failed),
        robots_blocked=run.items_robots_blocked,
        manifest_emitted=emit_manifest,
    )
    return manifest


def _empty_manifest(target_date: date, started: str) -> Manifest:
    """Manifest for Sundays / holidays where BOA returned the SPA shell."""
    run = RunInfo(
        started_at=started,
        ended_at=now_iso(),
        date=target_date.isoformat(),
        sumario_items_total=0,
        items_filtered_in=0,
        items_written=0,
        attribution=ATTRIBUTION_BOA,
    )
    return Manifest(source=SOURCE_BOA, run=run, items=[])
