# ADR-008 — Content scanning via staging Blob + Defender for Storage + promoter (Pattern F)

- **Status:** Accepted
- **Date:** 2026-06-03
- **Owners:** Data platform team
- **Related:** [ADR-005 OneLake folder structure](0005-onelake-folder-structure.md), [ADR-006 network posture](0006-bo-ingest-network-posture-option-a.md), [ADR-007 compute + naming](0007-compute-platform-confirmed-aca-jobs.md)

## Context

The pilot was about to deploy with the loader writing PDFs directly from `boe.es` to OneLake bronze. That's defensible — BOE is a government domain served over HTTPS — but it leaves a gap: the loader treats the source as fully trusted and ingests bytes without any content scan.

We decided that for an enterprise data platform handling external content, this gap should be closed before production rollout, even though the source-side threat is low.

**Constraint discovered during research**: Defender for Storage's on-upload malware scanning only works on standard Azure Storage Accounts (Blob + ADLS Gen2). It does **not** scan OneLake directly — OneLake is a Fabric-managed service abstraction; the underlying storage isn't an account we can enable Defender on.

So if we want malware scanning before bronze, we have to introduce a **staging Azure Storage Account** between the ACA Job and OneLake. That's Microsoft's documented DMZ pattern (`/azure/defender-for-cloud/defender-for-storage-configure-malware-scan` → "Use an intermediary storage account as a DMZ").

## Decision

Adopt **Pattern F**: ingest writes to a staging Azure Storage Account with Defender for Storage enabled; a separate scheduled ACA Job (the promoter) reads scan-result blob index tags every 5 minutes and copies clean blobs to OneLake bronze.

```text
       BOE (api.boe.es)
              │
              ▼
   ┌──────────────────────────────────┐
   │ ACA Job: caj-boe-daily            │
   │  Fetches PDFs                     │
   │  Validates host / CT / magic /    │
   │     size                          │
   └────────────────┬─────────────────┘
                    │ writes raw bytes + blob metadata
                    ▼
   ┌──────────────────────────────────┐
   │ Azure Storage Account             │
   │   storiginationdmz                │  ← NEW
   │     untrusted/                    │
   │     quarantine/                   │
   │                                  │
   │   Defender for Storage ENABLED   │
   │     on-upload scan               │
   │     index tag with verdict       │
   └────────────────┬─────────────────┘
                    │
                    │ (scan completes seconds to minutes later)
                    │ blob index tag set:
                    │   "Malware Scanning scan results" =
                    │     "No threats found" | "Malicious"
                    │
                    ▼ promoter polls every 5 min
   ┌──────────────────────────────────┐
   │ ACA Job: caj-promoter             │  ← NEW
   │  CRON every 5 min                 │
   │  Lists untrusted/ blobs           │
   │  For each:                        │
   │    "No threats found" →            │
   │       copy to OneLake;             │
   │       delete from untrusted        │
   │    "Malicious" →                   │
   │       move to quarantine/;         │
   │       log security event           │
   │    Pending →                       │
   │       skip; retry next run         │
   └────────────────┬─────────────────┘
                    │ clean files only
                    ▼
   ┌──────────────────────────────────┐
   │ OneLake — lh_esp_origination      │
   │   Files/bronze/boe/raw/...        │
   │   _manifests/year=.../day=.../    │
   │     _manifest.json                │
   └──────────────────────────────────┘
```

## Why Pattern F (vs the alternatives)

Three other patterns were considered:

| Pattern | What | Pro | Con |
|---------|------|-----|-----|
| **F — Scheduled promoter ACA Job** *(chosen)* | Second ACA Job runs every 5 min, checks index tags, promotes clean blobs | All-ACA (no Function App); decoupled ingest; ~5 min latency acceptable for a daily batch source | Two ACA Jobs to operate; promoter Job runs even on empty days (cheap, but extra invocations) |
| B — Event Grid + Function App | Defender's Event Grid event triggers a Function App that promotes near-real-time | Microsoft's canonical pattern; sub-minute latency | New compute platform (Function App); more components; Function App cold-start variability |
| E — Polling inside the same Job | Ingest Job uploads, then polls index tags for ~3 min, promotes inline | Single Job; simplest ops | Job runtime grows from 3s to ~3min; synchronous coupling; if Defender is slow we wait |
| G — Re-anchor bronze to staging blob | OneLake reads from the staging blob via a Fabric Shortcut; no promoter | No promoter; smallest moving parts | Big change to ADR-005's "bronze in OneLake" contract; reshapes downstream consumer assumptions |

**Pattern F wins** for our context because:

1. **Uniform compute** — adding the promoter as another ACA Job means one platform, one image-build pipeline, one observability story. Matches ADR-007's growth scenario (BOA, Endesa, REE, BORME all add their own Jobs the same way).
2. **Decoupled** — ingest performance doesn't depend on Defender's scan time.
3. **Low ops complexity** — a 5-minute cron on an idempotent Job is operationally trivial. No webhook auth, no Function-App-specific debugging.
4. **Latency acceptable** — for a once-a-day source, the difference between "PDFs in OneLake at 07:05" and "at 07:00" is meaningless.

## New resources to provision

Adding to ADR-007's existing list:

| # | Resource | Suggested name | Notes |
|---|----------|----------------|-------|
| 1 | **Azure Storage Account** | `storiginationdmz` (3–24 chars, lowercase alphanumeric; globally unique — suffix if taken) | StorageV2, LRS, in `rg-origination` |
| 2 | Container | `untrusted` | initial PDF landing |
| 3 | Container | `quarantine` | malicious blobs land here |
| 4 | Defender for Storage plan on `storiginationdmz` | — | enabled per-account (subscription-level enable would also work) — **admin handshake required** |
| 5 | Role assignment: UAMI → `Storage Blob Data Contributor` on `storiginationdmz` | — | ingest Job writes here |
| 6 | Role assignment: UAMI → `Storage Blob Data Reader` on `storiginationdmz` containers | — | promoter Job reads tags + bytes |
| 7 | **ACA Job — promoter** | `caj-promoter` | CRON `*/5 * * * *` (every 5 min); same UAMI as ingest |
| 8 | Container image — promoter | reuse `boe-ingest:latest` with a different entry point, OR new `boe-promoter:latest` | See "Image strategy" below |

Storage account naming: `storiginationdmz` is 16 chars, lowercase alphanumeric. If globally taken, suffix with random digits (e.g., `storiginationdmz01`).

## Image strategy

Two options for the promoter's container:

- **Same image, different entry**: keep `boe-ingest:latest`; CMD/entry switches between `python -m boe_ingest --date today` and `python -m boe_ingest.promoter` based on Job args. **Chosen** — simpler, one image to maintain, one CI pipeline.
- Separate image `boe-promoter:latest`: more code-organization separation but doubles the maintenance.

The ingest Python package gains a new submodule (`boe_ingest.promoter`) reusing the existing `OneLakeWriter`, plus a new `BlobReader` to enumerate staging blobs and check index tags.

## Manifest semantics — what changes

Today (pre-staging): the loader writes a single `_manifest.json` at the end of its run, in OneLake, listing all PDFs and their final state.

With staging in the path:

- **Ingest Job** writes PDFs to staging blob (with blob metadata: `identifier`, `section`, `departamento_codigo`, `published_at`, `url_pdf`, `sha256`, `size_bytes`). No manifest written yet — items are pending scan.
- **Promoter Job** is the manifest writer:
  - First run after ingest: enumerates the day's staging blobs; for each that has a final verdict, promotes (or quarantines) and updates the daily `_manifest.json` in OneLake by **upserting per-item records**.
  - Subsequent runs that find no new resolved blobs are no-ops.
  - The day's manifest reaches its "final" state when every item is either promoted or quarantined.

This makes the manifest incrementally written but eventually consistent. Downstream consumers reading the manifest see whatever has been resolved by promotion time. Schema stays at v1.0; we add an optional `scan_verdict` field per item (`"No threats found"` | `"Malicious"`).

## Data flow (per-day example)

```text
07:00 UTC — caj-boe-daily fires
  fetches sumario  →  3 MITECO Section 3 items pass filter
  uploads          →  storiginationdmz/untrusted/year=2026/month=06/day=04/{boe-id}.pdf  × 3
  blob metadata    →  identifier, section, departamento_codigo, sha256, size_bytes
  blob metadata    →  also uploads sumario.json
  Job exits        →  3s runtime, exit 0

07:00–07:02      — Defender scans each blob in-memory
                   index tag added: "Malware Scanning scan results" = "No threats found"
                   (or "Malicious" if a sig hits)

07:05 UTC — caj-promoter fires (next 5-min boundary)
  lists untrusted/ blobs in today's partition
  for each clean blob:
    copy to OneLake  →  lh_esp_origination/Files/bronze/boe/raw/year=2026/month=06/day=04/{boe-id}.pdf
    upsert manifest entry (creates manifest file if first item of the day)
    delete from untrusted/ (or set "promoted=true" index tag)
  promoter exits   →  ~10–30s runtime

every 5 min thereafter — caj-promoter fires; no-ops for the rest of the day
                         until tomorrow's ingest fires again
```

## Trade-offs accepted

- **Cost**: Defender for Storage has a per-account billing floor of roughly €10–15/mo regardless of GB scanned. At our volume that's the dominant new line item. Storage + Event Grid + extra ACA Job invocations are negligible.
- **Latency**: ingest → OneLake landing goes from a few seconds to up to 5 minutes. For a once-daily source, irrelevant.
- **Complexity**: from one Job to two; from one storage location to two; from one manifest writer to two coordinating writers. Real but manageable.
- **Defender enablement is admin-only**: enabling Defender for Storage on the new account requires `Owner` or `Contributor` on the subscription / storage account. **Pre-deploy admin handshake required.**

## Things we explicitly do *not* add

- Custom AV scanning in the loader (ClamAV in the image). Adds dependency and maintenance with no benefit over Defender's signature freshness.
- Network-layer threat scanning (Azure Firewall Premium IDPS). Out of scope; ADR-006 stays Option A.
- Full PDF parsing/sanitization at bronze. The bronze contract is "raw bytes from the source"; parsing belongs in silver.
- Per-blob signing / hash chains. SHA-256 in blob metadata + the manifest is sufficient audit.

## Triggers to revisit

1. **Defender for Storage discontinues** or the per-account cost floor grows past a threshold the team won't accept.
2. **Latency requirement tightens** to sub-minute → switch to Pattern B (Event Grid + Function App).
3. **OneLake gets native malware-scanning support** → drop the staging pattern entirely.
4. **The team decides BOE is "trusted enough"** that the scan layer is overhead → fall back to direct OneLake write (ADR-008 superseded).
5. **A real malicious file is ever detected** by Defender → strong signal the pattern is justified; tighten further (egress allowlist, possibly Option C).

## Reversibility

- **Adding the staging path** (today): medium effort — new storage account, new Defender plan, new promoter Job, refactor loader to write to blob instead of OneLake. Single-day change.
- **Removing the staging path** (rollback): low effort — point the loader back at OneLake; delete the staging account; disable Defender plan. Manifest schema is unchanged.
- **Switching from Pattern F to B** (post-launch): low — replace the promoter Job with a Function App, keep everything else.

## Implementation checklist

Tracked as the next steps after this ADR lands:

1. **Update deploy script** to provision the storage account, containers, Defender enablement, and the promoter Job.
2. **Refactor `boe_ingest`** to write PDFs to staging blob (new `BlobWriter` class). Manifest emission moves to the promoter.
3. **Add `boe_ingest.promoter`** module — enumerates staging, checks tags, copies to OneLake, upserts manifest.
4. **Same Dockerfile** — different CMD/args distinguish ingest vs promoter Jobs.
5. **Pre-deploy admin handshake**: platform team enables Defender for Storage on `storiginationdmz` (or subscription-wide). Microsoft.EventGrid provider must be registered in the subscription.

## Verification

When deployed:

1. Trigger `caj-boe-daily` manually. PDFs land in `storiginationdmz/untrusted/year=Y/month=M/day=D/`.
2. Within 1–2 minutes, blob index tag `Malware Scanning scan results` appears with value `No threats found` (verify via Storage Explorer or `az storage blob show --container-name untrusted --name ... --query "tags"`).
3. At the next 5-min boundary, `caj-promoter` fires. Blobs disappear from `untrusted/`; corresponding blobs appear in `lh_esp_origination/Files/bronze/boe/raw/year=Y/month=M/day=D/`. The day's `_manifest.json` exists with `scan_verdict: "No threats found"` per item.
4. Inject a known-bad test blob (e.g. EICAR test string saved as `eicar.pdf`) into `untrusted/`. Within 2 min Defender flags it `Malicious`. On the next promoter run, the blob moves to `quarantine/` (not OneLake). A Defender for Cloud security alert fires.

If all four pass, the scan layer is functional.
