# Week 1 — BOE Ingestion Recap

**Period:** 2026-06-01 → 2026-06-08 (end-to-end verification on 2026-06-08)
**Scope:** Renewable-energy slice of the Boletín Oficial del Estado (BOE) landing as raw PDFs in the team's Fabric OneLake lakehouse.

This document is the reference for what is deployed, how to use it, and what's next.

---

## 1. Headline

The BOE ingestion pipeline is **running end-to-end autonomously** in our enterprise Azure tenant. Every day Monday–Saturday at 07:00 UTC, a scheduled job fetches the day's BOE, filters to renewable-energy items in scope, writes the matching PDFs into a malware-scanned staging area, and a second scheduled job moves them into the team's Fabric lakehouse within minutes of Defender's scan completing.

End-to-end was verified on **2026-06-08 at 09:12 UTC**: a single promoter run promoted 17 BOE PDFs accumulated across days 04 / 05 / 06 / 08 into `lh_esp_origination` bronze, with per-day manifests upserted (`5 + 3 + 5 + 4 = 17` items). Staging container drained to zero, quarantine empty. Counts cross-checked against the structured logs:

```text
{"sumarios_copied": 0, "promoted": 17, "quarantined": 0, "pending": 0, "skipped": 0, "event": "promoter_run_done"}
```

The architecture is documented in 8 architecture decision records and can be extended to additional sources (BOA, Endesa, REE) without changing the underlying platform.

---

## 2. What we shipped

### 2.1 Architecture decisions

Eight decisions, each captured as an ADR with the reasoning, alternatives considered, and the conditions under which it would be revisited. The key calls:

- **Compute:** Azure Container Apps Jobs over Azure Functions (multi-source growth path, container-native, no scaling cliff).
- **Region:** North Europe (co-located with the Fabric capacity — minimises egress and OneLake write latency).
- **Network posture for BOE:** Option A (public-tier ACA with managed-identity auth only) — appropriate for a fully public open-data source. A pre-positioned upgrade plan to Option C (VNet-injected + private endpoints) is documented and ready to execute if/when triggers fire (Endesa credentials, audit, policy change).
- **Auth:** User-assigned managed identity (`id-origination`); no secrets anywhere.
- **Filtering:** Section III + departamento códigos 9575 / 9593 (MITECO). Structural filter, no keyword regex.
- **Storage layout:** Medallion (bronze / silver / gold) inside the team's existing `lh_esp_origination` lakehouse; bronze partitioned by source first then by date (Hive style).
- **Content scanning:** "DMZ" pattern — ingest writes to a staging Azure Storage Account where Defender for Storage scans every upload; only clean blobs are promoted to OneLake bronze; infected blobs go to quarantine.
- **Apify evaluation:** Rejected for BOE (no anti-bot / JS / captcha to bypass); re-evaluation deferred to Endesa onboarding when its strengths become relevant.

### 2.2 Python package — `src/boe_ingest/`

A small, single-purpose Python 3.12 package (~150 LOC of business logic):

| Module | What it does |
|---|---|
| `__main__.py` | argparse CLI: `--date YYYY-MM-DD` / `--date=today` / `--from YYYY-MM-DD --to YYYY-MM-DD`. Selects writer (BlobWriter for staging, LocalWriter for `--out-dir`, OneLakeWriter for direct mode). |
| `orchestrator.py` | One-day pipeline: fetch sumario → walk items → apply relevance filter → fetch each PDF over HTTP/2 with retries → write to staging with per-blob metadata. |
| `sumario.py` | BOE Datos Abiertos API client. Validates HTTP status + inner status code. Walks nested sección → departamento → (epígrafe →) item tree. |
| `relevance.py` + `relevance.yaml` | Filter rules. Currently: section III with departamento códigos 9575 and 9593. |
| `fetcher.py` | Async PDF fetcher (httpx + tenacity) with concurrency cap and throttle. |
| `robots.py` | Honours `boe.es/robots.txt`. |
| `blob.py` | Azure Blob writer for staging. ASCII-folds Spanish characters in blob metadata (Azure rejects non-ASCII in metadata values). |
| `onelake.py` | OneLake ABFS writer for direct mode and for the promoter. ManagedIdentityCredential in Azure; DefaultAzureCredential locally. |
| `promoter.py` | Reads each staging blob's Defender scan-result tag; clean → copy to OneLake + upsert daily manifest; malicious → move to quarantine; pending → skip. Idempotent and crash-safe (copy-then-delete semantics). |
| `manifest.py` | Pydantic v2 schemas for the daily manifest. |
| `config.py` | Pydantic settings (env-driven). |

Console scripts (defined in `pyproject.toml`):
- `boe-ingest` → runs the daily / backfill orchestrator.
- `boe-promoter` → runs the promoter loop.

Tests pass on the calibration set: 14 positives (10 MITECO + 4 MPTYMD) + 9 negatives, 100% precision and recall.

### 2.3 Container image

Multi-stage `python:3.12-slim` build (~150 MB). Hashed lockfile, non-root user, no dev dependencies in the runtime stage. Image is in our ACR at:

```text
acrorigination.azurecr.io/boe-ingest:latest
```

### 2.4 Azure resources (all in `rg-origination`, North Europe)

| Resource | Name | Purpose |
|---|---|---|
| Resource group | `rg-origination` | Single RG for the whole pipeline |
| User-assigned managed identity | `id-origination` | Single identity used by all ACA Jobs |
| Container registry | `acrorigination` | Hosts `boe-ingest` image |
| Container Apps environment | `cae-origination` | Consumption profile, public-tier (per Option A network posture) |
| ACA Job | `caj-boe-daily` | Scheduled daily ingest (see §3.1) |
| ACA Job | `caj-boe-backfill` | On-demand backfill for date ranges |
| ACA Job | `caj-promoter` | Scheduled promotion of clean blobs to OneLake |
| Storage account (DMZ) | `storiginationdmz` | Staging area for Defender to scan before OneLake |
| ↳ container `untrusted` | | Where ingest writes; promoter reads scan tags here |
| ↳ container `quarantine` | | Where promoter moves blobs flagged malicious |
| Log Analytics workspace | `log-origination` | ACA Console + System logs from all Jobs |
| Action group | `ag-origination` | Reserved for alert rules (none active in pilot) |

### 2.5 OneLake destination

Workspace: **Central Data & Integration (DEV)** (mirrors planned for STG and PROD)
Lakehouse: **`lh_esp_origination`**

Bronze layout:

```text
Files/bronze/boe/raw/year=YYYY/month=MM/day=DD/
    BOE-A-2026-NNNNN.pdf       (one per filtered disposition)
    BOE-B-2026-NNNNN.pdf       (anuncios B and C subsections)

Files/bronze/boe/raw/_manifests/year=YYYY/month=MM/day=DD/
    _manifest.json             (per-run telemetry + per-item metadata)
```

Sibling folders `Files/bronze/{boa,endesa,ree}/raw/...` are pre-staged for their respective workstreams.

### 2.6 Identity and access

UAMI `id-origination` (objectId `7058e3ff-2ddf-4f12-8db7-80e6688c84e3`) holds:

| Role | Scope | Purpose |
|---|---|---|
| `AcrPull` | `rg-origination` (inherited) | Pulls the container image |
| `Storage Blob Data Owner` | `storiginationdmz` storage account | Reads / writes / deletes blobs and reads scan-result index tags (the tag-read action is missing from `Storage Blob Data Contributor` in this tenant) |
| Workspace Contributor on `Central Data & Integration (DEV)` | via Fabric `Central Members (DEV)` security group | Writes to OneLake bronze |

### 2.7 Content scanning (Defender for Storage)

Plan: **Standard / `PerStorageAccount` subplan** at the subscription level. Per-account configuration on `storiginationdmz`:

- `malwareScanning.onUpload.isEnabled: true` (no monthly cap)
- `sensitiveDataDiscovery.isEnabled: true`
- `overrideSubscriptionLevelSettings: true`

Plumbing auto-provisioned by Defender: Event Grid system topic `storiginationdmz-893da9cd-3692-42c3-b61a-f28228c3cf9e` with subscription `StorageAntimalwareSubscription` listening to `Microsoft.Storage.BlobCreated` and `Microsoft.Storage.BlobRenamed`.

### 2.8 Documentation

| File | Purpose |
|---|---|
| `README.md` | Repo overview, ADR index, operational-plans index |
| `docs/decisions/0001..0008-*.md` | Architecture decision records |
| `docs/operational/network-upgrade-plan-option-c.md` | Pre-positioned plan for upgrading the network posture from Option A to Option C, with full risk analysis and an Apify-as-alternative steelman |
| `boe-dataset-deep-dive.html`, `boa-dataset-deep-dive.html` | Source-of-truth descriptions of the BOE and BOA corpora |

---

## 3. Current operational state

### 3.1 When the jobs actually run

A typical 24-hour timeline (all times UTC, Mon–Sat):

```text
─┬─ 07:00 ─────────┬─ 07:15 ──────────┬─ 07:45 ──────────┬─ rest of day ──────────
 │  caj-boe-daily  │  caj-promoter    │  caj-promoter    │  Nothing scheduled.
 │  ingests BOE    │  (primary run)   │  (safety net)    │  Cluster is idle.
 │  ↓              │                  │                  │
 │  PDFs land in   │                  │                  │
 │  staging,       │                  │                  │
 │  Defender       │                  │                  │
 │  scans during   │                  │                  │
 │  the next       │                  │                  │
 │  ~5-15 minutes  │                  │                  │
 │                 │  → Promotes      │  → Promotes      │
 │                 │    tagged PDFs   │    anything      │
 │                 │    to OneLake    │    still pending │
 │                 │    + manifest    │    from 07:15    │
```

**Concrete recurring schedule:**

| Job | Cron | When | Effective work per day |
|---|---|---|---|
| `caj-boe-daily` | `0 7 * * 1-6` | 07:00 UTC, Mon–Sat (Sunday correctly skipped, BOE doesn't publish) | 1 run, ~30s, fetches today's BOE + writes matching PDFs to staging |
| `caj-promoter` | `15,45 7 * * 1-6` | 07:15 and 07:45 UTC, Mon–Sat | 2 runs/day — first catches the typical 5–15 min Defender scan latency; second is a safety net for anything still pending at 07:15. Total: 12 runs/week. |
| `caj-boe-backfill` | None (manual only) | — | 0 unless triggered |

### 3.2 Why two promoter runs, not a polling cron?

The promoter's job is to drain the staging container after Defender finishes tagging. Defender's tagging latency is **5–15 minutes** for our blob sizes (observed; Microsoft's documented upper bound is 30 min to 3 hours for unusually large or complex blobs).

The daily Job uploads PDFs once per day, around 07:00 UTC. So promoter polling-style cron is over-engineered:

- A `*/5 * * * *` cron (the original pilot setting) was 288 runs/day, **287 of which were no-ops** — pure log noise.
- A `*/15 * * * *` cron would be 95 runs/day, 94 no-ops. Still wasteful.
- **Two runs aligned to the upload window (07:15 + 07:45 UTC)** catches the realistic Defender latency with one belt-and-one-suspender. 12 runs/week total.

If Defender ever takes longer than 45 minutes (i.e., past 07:45 UTC), those PDFs sit in staging until tomorrow's 07:15 run — they get picked up then. Acceptable for a daily corpus where downstream consumers don't care about same-hour freshness. For real backfills triggered ad-hoc via `caj-boe-backfill`, just trigger `caj-promoter` manually too once Defender's had time to scan.

### 3.3 Cron jobs (detail)

Three ACA Jobs deployed; two run on a schedule, one is manual.

#### `caj-boe-daily` — Daily BOE ingest

| Field | Value |
|---|---|
| Schedule | `0 7 * * 1-6` (07:00 UTC, Monday through Saturday — matches BOE publication days) |
| Command (image entrypoint) | `python -m boe_ingest` |
| Args | `--date=today` |
| Identity | UAMI `id-origination` |
| Image | `acrorigination.azurecr.io/boe-ingest:latest` |
| Replica timeout | 1800s (30 min) |
| Parallelism | 1 |
| Writer mode | Staging (env var `STG_ACCOUNT_NAME` set) — writes to `storiginationdmz/untrusted/...` with per-blob metadata. Does NOT write the manifest (the promoter owns that). |

What it does on each run:

1. Fetches `https://www.boe.es/datosabiertos/api/boe/sumario/{YYYYMMDD}`.
2. Walks the sumario tree (sección → departamento → epígrafe → item).
3. Applies the relevance filter from `relevance.yaml`.
4. For each surviving item, fetches its PDF (concurrency-capped, throttled, robots.txt-respecting, retries with exponential backoff).
5. Writes each PDF to `storiginationdmz/untrusted/bronze/boe/raw/year=YYYY/month=MM/day=DD/{identifier}.pdf` with blob metadata: identifier, section, departamento_codigo, departamento (ASCII-folded), published_at, sha256, size_bytes, url_pdf, url_xml, url_html.
6. The unfiltered BOE sumario is **fetched in memory only and NOT persisted** anywhere — it contains 240+ items per day outside our scope (judicial, civil-service, anuncios B/C) that we don't need.

#### `caj-promoter` — Promote clean blobs from staging to OneLake

| Field | Value |
|---|---|
| Schedule | `15,45 7 * * 1-6` (07:15 and 07:45 UTC, Mon–Sat) |
| Command | `boe-promoter` (console script) |
| Args | (none) |
| Identity | UAMI `id-origination` |
| Image | `acrorigination.azurecr.io/boe-ingest:latest` |
| Replica timeout | 600s (10 min) |
| Parallelism | 1 |

What it does on each run:

1. Lists all blobs in `storiginationdmz/untrusted/`.
2. For each blob:
   - Reads the `Malware Scanning scan results` blob index tag (written by Defender).
   - `No threats found` → downloads the blob, writes to OneLake at the same path, deletes from staging.
   - `Malicious` → copies to `quarantine/`, deletes from `untrusted/`, emits a `scan_malicious` warning log.
   - Missing tag (scan still pending) → logs `scan_pending` and skips; next run will pick it up.
3. For each day touched, upserts the daily manifest at `Files/bronze/boe/raw/_manifests/year=YYYY/month=MM/day=DD/_manifest.json` — merge by item identifier, last write wins.

Idempotent and crash-safe by design: "copy then delete" semantics mean a crash between copy and delete just makes the next run re-copy the same bytes and try the delete again.

#### `caj-boe-backfill` — On-demand backfill

| Field | Value |
|---|---|
| Schedule | None (manual trigger only) |
| Command | `python -m boe_ingest` |
| Args | Pass at trigger time via `az containerapp job start`, e.g. `--from=2026-05-01 --to=2026-05-31` |
| Replica timeout | 3600s (60 min) |

Same code path as the daily Job, different argv. Idempotent across re-runs.

### 3.2 Destinations

| Stage | Where the bytes live | Lifetime |
|---|---|---|
| 1. Ingest output | `storiginationdmz/untrusted/bronze/boe/raw/year=.../day=.../*.pdf` (+ Defender scan tag written async) | Until promoted or quarantined; should not exceed a few minutes in normal operation |
| 2. Quarantine | `storiginationdmz/quarantine/...` | Indefinite — kept for forensics |
| 3. Bronze (canonical) | OneLake `lh_esp_origination.Lakehouse/Files/bronze/boe/raw/year=.../day=.../*.pdf` | Indefinite — this is the canonical archive downstream consumers read from |
| 4. Manifest | OneLake `lh_esp_origination.Lakehouse/Files/bronze/boe/raw/_manifests/year=.../day=.../_manifest.json` | Indefinite — one per ingest day, upserted as items land |

### 3.3 Identity flow at runtime

```text
ACA Job replica
   │  Container starts. The Azure metadata service IDENTITY_ENDPOINT env var
   │  is auto-injected by ACA. Our code detects it and uses
   │  ManagedIdentityCredential(client_id=<id-origination clientId>).
   ▼
Token acquired for storiginationdmz
   │  Azure validates the UAMI has Storage Blob Data Owner on the account.
   ▼
PUT blob with metadata → 201 Created
   │  Tag-read later by the promoter → 200 OK (Owner role includes
   │  the blobs/tags/read data action; Contributor in this tenant does not).
   ▼
Promoter copies to OneLake using the same UAMI
   │  The UAMI is a workspace Contributor via Fabric security group
   │  "Central Members (DEV)".
   ▼
OneLake accepts the write
```

---

## 4. Usage

All commands below assume you're authenticated to Azure CLI with access to subscription `53cc82cf-f636-425d-8e25-f37b6bb8ef8f` and resource group `rg-origination`.

### 4.1 Trigger today's daily ingest manually

```bash
az containerapp job start \
  --name caj-boe-daily \
  --resource-group rg-origination
```

The Job uses the args saved on the Job definition (`--date=today`). Returns an execution name like `caj-boe-daily-xxxxxxx`.

### 4.2 Trigger an ingest for a specific past day

Update the args (one-shot), then start:

```bash
az containerapp job update \
  --name caj-boe-daily \
  --resource-group rg-origination \
  --args="--date=2026-05-27"

az containerapp job start \
  --name caj-boe-daily \
  --resource-group rg-origination
```

**Important:** after the run, revert the args back to `--date=today` or the next scheduled cron will re-run that historical date instead of today's BOE:

```bash
az containerapp job update \
  --name caj-boe-daily \
  --resource-group rg-origination \
  --args="--date=today"
```

### 4.3 Backfill a date range

Use the dedicated backfill Job — it has a larger timeout (60 min) and is named for this purpose:

```bash
az containerapp job update \
  --name caj-boe-backfill \
  --resource-group rg-origination \
  --args="--from=2026-05-20 --to=2026-05-27"

az containerapp job start \
  --name caj-boe-backfill \
  --resource-group rg-origination
```

Backfill is idempotent — re-running the same range overwrites the same paths with the same byte content.

### 4.4 Trigger the promoter manually

Usually no need — cron runs every 5 min. But you can force a sweep:

```bash
az containerapp job start \
  --name caj-promoter \
  --resource-group rg-origination
```

### 4.5 Watch an execution to completion

```bash
RUN=caj-boe-daily-xxxxxxx
watch -n 5 "az containerapp job execution show \
  -n caj-boe-daily -g rg-origination \
  --job-execution-name $RUN \
  --query properties.status -o tsv"
```

Or poll once and exit on terminal state:

```bash
RUN=caj-boe-daily-xxxxxxx
while :; do
  s=$(az containerapp job execution show -n caj-boe-daily -g rg-origination --job-execution-name "$RUN" --query "properties.status" -o tsv)
  echo "[$(date -u +%H:%M:%S)] $s"
  [[ "$s" == "Succeeded" || "$s" == "Failed" ]] && break
  sleep 10
done
```

### 4.6 Read structured logs from a specific execution

```bash
WS=$(az monitor log-analytics workspace show -n log-origination -g rg-origination --query customerId -o tsv)

az monitor log-analytics query \
  --workspace "$WS" \
  --analytics-query "ContainerAppConsoleLogs_CL
    | where ContainerGroupName_s startswith 'caj-boe-daily-xxxxxxx'
    | where Log_s startswith '{'
    | order by TimeGenerated asc
    | project Log_s" \
  -o tsv
```

All meaningful events are emitted as structured JSON lines via `structlog`. Key event names:

| Event | When |
|---|---|
| `run_start`, `run_done` | Start/end of an ingest run |
| `sumario_fetch_start`, `sumario_fetch_ok` | BOE API call |
| `sumario_items_total` | Count of items in the unfiltered sumario |
| `items_filtered_in` | Count of items that passed the filter |
| `robots_loaded`, `robots_blocked` | robots.txt handling |
| `blob_write_ok` | PDF or sumario successfully written to staging |
| `promoter_run_start`, `promoter_run_done` | Promoter loop boundaries |
| `scan_pending` | Defender hasn't tagged this blob yet (will retry next cron tick) |
| `blob_promoted` | Clean blob copied to OneLake bronze |
| `sumario_promoted` | (Legacy — sumario uploads have been removed; this event only appears for old staging blobs not yet drained) |
| `scan_malicious` | Defender flagged the blob as infected; promoter moved it to quarantine |
| `manifest_upserted` | Promoter upserted the day's manifest in OneLake |
| `onelake_write_ok` | Anything successfully written to OneLake (paths, byte counts visible) |

### 4.7 List what's currently in staging

```bash
az storage blob list \
  --account-name storiginationdmz \
  --container-name untrusted \
  --auth-mode login \
  --query "[].{name:name, size:properties.contentLength, lastModified:properties.lastModified}" \
  --output table
```

(Your Entra user must have `Storage Blob Data Reader` or higher on the account to use `--auth-mode login`.)

### 4.8 Read a manifest from OneLake

Easiest via a Fabric notebook in `Central Data & Integration (DEV)`:

```python
import json
with open("/lakehouse/default/Files/bronze/boe/raw/_manifests/year=2026/month=06/day=04/_manifest.json") as f:
    manifest = json.load(f)
print(manifest["run"])
for item in manifest["items"]:
    print(item["identifier"], item["section"], item["departamento_codigo"], item["pdf_path"])
```

### 4.9 Rebuild the container image after a code change

```bash
az acr build \
  --registry acrorigination \
  --image boe-ingest:latest \
  --file containers/boe.Dockerfile \
  .
```

The `:latest` tag is what the ACA Jobs reference; the next Job execution will pull the new image automatically.

---

## 5. What was verified end-to-end

| Component | Status | Evidence |
|---|---|---|
| UAMI authenticates to staging account | ✓ | `ManagedIdentityCredential.get_token_info succeeded` log lines, blob writes returning 201 Created |
| Daily Job ingests BOE Mon–Sat at 07:00 UTC | ✓ | Autonomous runs on 2026-06-04 / 05 / 06 / 08. Cron correctly skipped Sunday 2026-06-07. Each run filtered 247 sumario items down to the expected 3–5 MITECO renewable-energy dispositions. |
| Filter is correct | ✓ | Calibration tests passing (14/14 positives, 9/9 negatives); production runs identified the expected MITECO dispositions per day |
| Spanish characters in metadata don't crash Azure | ✓ | `_ascii_fold` in `blob.py` ("Transición" → "Transicion"); zero `InvalidMetadata` errors after the fix |
| Promoter authenticates and reads blob index tags | ✓ | Promoter runs successfully read Defender tags via `get_blob_tags()` after `Storage Blob Data Owner` was granted |
| Defender writes scan-result tags within minutes | ✓ | After the platform-team re-toggle on 2026-06-08 and the scanner identity's role assignment landed, tags appeared on all 17 staged blobs |
| Promoter promotes clean blobs to OneLake bronze | ✓ | Single promoter run on 2026-06-08 09:12 UTC promoted 17 PDFs to `lh_esp_origination` bronze; 17 `blob_promoted` events + 17 `onelake_write_ok` events |
| Manifests are written + upserted per day | ✓ | `_manifest.json` written to `_manifests/year=2026/month=06/day=DD/` for days 04 (5 items), 05 (3 items), 06 (5 items), 08 (4 items) |
| Promoter is idempotent + crash-safe | ✓ | Cron has been ticking every 5 min since 2026-06-04 with no spurious side-effects; "copy then delete" semantics survived all the Defender misconfig runs |
| End-to-end pipeline: BOE → staging → Defender → promoter → OneLake | **✓** | Verified 2026-06-08 09:12 UTC: `promoted: 17, quarantined: 0, pending: 0, skipped: 0`; staging container drained to 0 blobs |

---

## 6. Open items and next steps

### 6.1 Immediate (next 24h, no action required)

End-to-end is verified. The 07:00 UTC daily Job + 5-min promoter cron will run autonomously tomorrow and every Mon–Sat thereafter. Tomorrow's spot-check: confirm that by ~07:20 UTC, tomorrow's filtered PDFs are visible in `lh_esp_origination.Lakehouse/Files/bronze/boe/raw/year=2026/month=06/day=09/` and the manifest exists.

### 6.2 Quick wins worth doing this week

- **Remove the now-redundant `Storage Blob Data Contributor`** assignment on the staging account — `Storage Blob Data Owner` is a superset. Cosmetic.

### 6.3 Week 2 priorities

**Scope-in:**

1. **Endesa source scoping.** Decision points: collection mode (HTML scrape vs partner API vs Apify hosted), terms-of-service review, source-shape document analogous to the BOE deep-dive. Likely triggers a re-evaluation of the network posture (Option A → Option C) because authenticated sources change the risk profile substantially — see `docs/operational/network-upgrade-plan-option-c.md`.
2. **REE source scoping.** Less urgent than Endesa; shape unknown.
3. **Manual verification of week 1's bronze partitions.** A Fabric notebook in the workspace reads two days' worth of PDFs by SHA-256 and confirms the manifest counts match. Proves the contract to downstream consumers.

**Deferred from week 1 (consciously, to keep pilot scope tight):**

4. **GitHub Actions CI.** `uv sync --frozen` drift check + `az acr build` on push. Currently we rebuild manually with `az acr build`; this is fine for the cadence but adds friction.
5. **Dependabot.** Weekly cadence for PyPI + GitHub Actions ecosystems, security alerts.
6. **ACR image scanning.** Defender for Containers / built-in ACR scanning fails the pipeline on critical/high CVEs.
7. **Alert rules.** Action group `ag-origination` is provisioned but empty. Add alerts for: daily Job failure two days running, promoter cron not running for >15 min, quarantine container non-empty.
8. **Per-source UAMI carve-out.** Currently `id-origination` is the single identity for the whole pipeline. As we add Endesa and REE, splitting the identity per source improves blast-radius isolation.
9. **Cleanup of the legacy `Storage Blob Data Contributor` assignment.** `Owner` is a superset; Contributor is redundant. Cosmetic.

**Watch-only:**

10. **Network upgrade triggers.** If any of the documented Option C triggers fires (Endesa credentials enter scope, Azure Policy denying public-network resources, audit feedback, real incident), pull the trigger documented in `docs/operational/network-upgrade-plan-option-c.md` — ~1 day infra work, ~€110/mo recurring.

### 6.4 Known gotchas to be aware of

- **`Storage Blob Data Contributor` in this tenant does not include `blobs/tags/read`.** The promoter needs that to read Defender's scan verdict. We solved this with `Storage Blob Data Owner` (wildcard `blobs/*`). When provisioning future identities for blob-tag use cases, default to Owner or a custom role with the explicit action.
- **Defender for Storage configuration silently no-ops if the user PUT'ing the settings hits an ABAC role-assignment restriction.** The settings show `isEnabled: true` but `operationStatus.code: MissingPermissions` because the scanner identity's role assignment couldn't be created. Fix: someone without that ABAC restriction must re-toggle the settings (just re-PUT the same JSON). This bit us hard on 2026-06-08 — burned a day diagnosing why scans weren't producing tags despite the wiring looking healthy. Always check `properties.malwareScanning.operationStatus` after enabling Defender.
- **The Defender blob-index tag key is `Malware Scanning scan result` (singular, no trailing 's').** Microsoft's docs are inconsistent (some places say "scan results"). Verify against an actual tagged blob via the portal before hardcoding the key in code. Our promoter had the wrong constant for a week.
- **ACA Job args with spaces don't round-trip cleanly.** `--date today` gets stored as a single token with embedded whitespace and Python's argparse rejects it. Always use the equals form: `--date=today`, `--from=2026-05-01`.
- **`uv sync` editable installs break multi-stage Docker.** The editable `.pth` file points back to `/build/src/`, which doesn't exist in the runtime stage. Always use `uv sync --frozen --no-dev --no-editable` in container builds.
- **Defender activation window is up to 24 hours** after first-time subscription-level enablement of the Standard plan. After that, per-blob tags appear within minutes.
- **Azure blob metadata values must be ASCII.** Spanish text (`Transición`) fails with `InvalidMetadata`. We ASCII-fold values before sending; the full Unicode original is preserved in the per-item manifest in OneLake.

---

## 7. Pointers

- **Code:** `src/boe_ingest/`
- **Container build:** `containers/boe.Dockerfile`
- **Tests:** `tests/` (run with `uv run pytest tests/ -q`)
- **Architecture decisions:** `docs/decisions/0001..0008-*.md`
- **Network upgrade pre-positioned plan:** `docs/operational/network-upgrade-plan-option-c.md`
- **Subscription:** `53cc82cf-f636-425d-8e25-f37b6bb8ef8f`
- **Resource group:** `rg-origination` (North Europe)
- **UAMI principal ID:** `7058e3ff-2ddf-4f12-8db7-80e6688c84e3`
- **UAMI client ID:** `c9694523-1ba3-4bb1-abca-964ca710f937`
- **Container image:** `acrorigination.azurecr.io/boe-ingest:latest`
- **OneLake destination:** `Central Data & Integration (DEV)` workspace → `lh_esp_origination` lakehouse → `Files/bronze/boe/raw/...`
