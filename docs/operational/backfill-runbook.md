# Backfill runbook (BOE / BOA → 2019)

How to backfill the historical renewable slice of BOE and BOA into OneLake bronze
without touching live daily ingestion. Reuses the existing
staging → Defender → promoter → OneLake pipeline; backfilled files land in the
same `year=/month=/day=/` bronze partitions as daily data (no collision,
idempotent).

See [how-the-pipeline-works.md](../architecture/how-the-pipeline-works.md) for the
pipeline itself; this doc is the operational procedure for the historical load.

---

## Key idea: a separate backfill filter, daily untouched

The daily relevance filters (`relevance.yaml`) are calibrated for **today's**
government taxonomy. Department/ministry names + códigos have drifted since 2019,
so today's filter matches almost nothing before ~2024. The backfill therefore
uses a **separate** `relevance.backfill.yaml` per source — a historical-aware
**superset** selected by `--backfill` / `--relevance-profile=backfill`. The daily
Jobs never pass that flag, so their behavior is byte-identical (guarded by the
unchanged `test_relevance.py` / `test_boa_relevance.py`).

---

## Procedure per source

### 1. Enumerate the historical entities (certainty, not guessing)

```bash
uv run python scripts/enumerate_departamentos.py boe 2019 2025
uv run python scripts/enumerate_departamentos.py boa 2019 2025
```

Read-only, sumario-only (no downloads). Prints every distinct ministry/department
that appears in the relevant sections, with first/last-seen dates. Department
names are piecewise-constant (they change only on government reorganizations), so
monthly sampling catches every name that ever existed.

### 2. Classify + confirm, then lock the filter

From the catalog, identify the renewable lineage and **confirm the scope with the
data owner** before writing the filter. Then encode it in
`src/{boe,boa}_ingest/relevance.backfill.yaml`:

- **BOE** — códigos drift, names are stable, so use case-insensitive
  `issuer_name_patterns` (inline `(?i)`) on top of the current códigos. Confirmed
  scope (2026-06): `(?i)transición ecológica` + `(?i)política territorial`
  (covers MITECO 9566→9575 and territorial 9561→9523→9593); Industria/Economía
  ministries excluded.
- **BOA** — one rule per historical department name (passes_filter ORs rules).

Add the historical eras to the calibration fixtures
(`tests/fixtures/*_backfill_calibration_set.yaml`) and confirm
`uv run pytest tests/ -q` is green (the backfill test must be 100% across eras;
the daily tests must stay untouched and green).

Rebuild the image so the updated YAML ships in it:

```bash
az acr build --registry acrorigination --image origination-ingest:latest \
  --file containers/origination-ingest.Dockerfile .
```

### 3. Run, newest → oldest, one year at a time

```bash
scripts/backfill.sh boe 2026 2026     # the 2026 pre-go-live gap first
scripts/backfill.sh boe 2025 2025     # then descend, verifying each year
scripts/backfill.sh boe 2024 2024
...
scripts/backfill.sh boe 2019 2019
# (or a span at once: scripts/backfill.sh boe 2025 2019)
```

The driver clamps the newest chunk to the day before that source's daily go-live
(BOE `2026-06-04`, BOA `2026-06-08`), so backfill never overlaps live data. It
updates `caj-{src}-backfill` args to `--backfill=FROM:TO` (single token — `az
--args="a b c"` collapses to one argv element, so the colon form is required),
starts the Job, and polls to completion before the next chunk.

### 4. Verify

- Staging drains automatically: the **promoter cron** (6×/day, mornings) scans +
  promotes the backfill's staged blobs to OneLake on its next tick. To drain
  immediately instead of waiting, trigger it once:
  `az containerapp job start -n caj-promoter -g rg-origination`.
- Confirm OneLake landing (per-day partitions + `_manifest.json`), e.g. count
  PDFs under `bronze/boe/raw/year=YYYY/month=MM/` and check a manifest.
- The daily Jobs keep running unaffected throughout (different filter, different
  dates).

---

## Reference

| Item | Value |
|---|---|
| Backfill Jobs | `caj-boe-backfill`, `caj-boa-backfill` (Manual trigger, 7200s timeout) |
| Daily go-live (clamp) | BOE `2026-06-04`, BOA `2026-06-08` |
| Backfill filter flag | `--backfill=FROM:TO` (implies the backfill profile) |
| Filter configs | `src/{boe,boa}_ingest/relevance.backfill.yaml` |
| Enumeration helper | `scripts/enumerate_departamentos.py <source> <from_year> <to_year>` |
| Driver | `scripts/backfill.sh <source> <start_year> <end_year>` |

## Idempotency + resume

Every chunk is idempotent — re-running a year overwrites the same partitions with
identical bytes; the manifest upsert merges by identifier. If a year fails
mid-run, just re-run that year: `scripts/backfill.sh boe 2022 2022`.

## Volume (observed)

The BOE 2026 gap (Jan 1 – Jun 3) produced ~600 filtered PDFs (~120/month), so a
full year is ≈1,400 PDFs and 2019–2025 ≈10k PDFs (~2–3 GB) — comfortably within
Defender's scan throughput and the monthly cap. Expect a backfill burst to sit in
staging until the next promoter tick drains it.
