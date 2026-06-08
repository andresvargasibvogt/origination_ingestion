# BOE + BOA → OneLake

Daily ingestion of the renewable-energy slice of two Spanish official gazettes into Microsoft Fabric OneLake:

- **BOE** (Boletín Oficial del Estado) — state-competence projects (>50 MW peninsular, multi-CCAA, offshore).
- **BOA** (Boletín Oficial de Aragón) — Aragón regional-competence projects (≤50 MW within the CCAA).

Both loaders run as Azure Container Apps Jobs in Spain Central, write to OneLake via managed identity, and feed downstream extraction / linking tools that live outside this repo.

## Status

- Architecture plan: approved (see `~/.claude/plans/i-need-to-scrape-lively-boole.md` for the working plan).
- Decision records: ADR-001 through ADR-004 below.
- Implementation: scaffolding pending team confirmation.

## Decision records

| ID | Title | Summary |
|----|-------|---------|
| [ADR-001](docs/decisions/0001-compute-platform.md) | Compute platform | Azure Container Apps Jobs in Spain Central. Apify (maximalist mode) rejected for this corpus. |
| [ADR-002](docs/decisions/0002-network-posture.md) | Network posture | Default Option C (VNet-injected + workspace private link). Downgrade to Option A only if three policy checks all say no. |
| [ADR-003](docs/decisions/0003-supply-chain-controls.md) | Supply-chain controls | Must-have: hashed lockfile, minimal base image, scoped RBAC, Dependabot, ACR scanning. Should-haves and optional layered above. |
| [ADR-004](docs/decisions/0004-boa-discovery-method.md) | BOA discovery method | Sequenced: email publisher → RSS spike → fallback to in-container Apify SDK / Crawlee local (default), bare Playwright (regression), or Narrow Apify hosted (contingent). |
| [ADR-005](docs/decisions/0005-onelake-folder-structure.md) | OneLake folder structure | Medallion (bronze/silver/gold) inside each existing dev/stg/prod workspace; bronze partitioned by source first, then by date; loader writes only to bronze. |
| [ADR-006](docs/decisions/0006-bo-ingest-network-posture-option-a.md) | Network posture for BOE loader | **Option A** (public-tier ACA, MI auth only) for the BOE loader specifically — overrides ADR-002 default of Option C. Triggers to revisit + migration path documented. |
| [ADR-007](docs/decisions/0007-compute-platform-confirmed-aca-jobs.md) | Compute platform reconfirmed | Stays with **ACA Jobs** (vs reconsidering Functions) because of the multi-source growth path (BOA, Endesa, REE, etc.). Includes service inventory + az deploy checklist. |
| [ADR-008](docs/decisions/0008-content-scanning-staging-pattern-f.md) | Content scanning via staging Blob + Defender + promoter | **Pattern F** — ingest writes to a staging Azure Storage Account with Defender for Storage enabled; a separate scheduled ACA Job (promoter) reads scan-result blob tags every 5 min and copies clean blobs to OneLake bronze. Manifest semantics + verification + reversibility documented. |

## Operational plans

| Doc | Title | Status |
|-----|-------|--------|
| [Week 1 recap](docs/operational/week-1-recap.md) | What we shipped in week 1 (2026-06-01 → 2026-06-05): architecture, resources, cron jobs, destinations, usage runbook, week 2 next steps | Current |
| [Option C upgrade plan](docs/operational/network-upgrade-plan-option-c.md) | Pre-positioned plan for migrating Option A → Option C (VNet + private endpoints + workspace private link) | Not executed; ready to run when a trigger fires (Endesa creds, audit, policy change, incident) |

## Source deep-dives

The conceptual business requirements live in two HTML deep-dives at the repo root:

- `boe-dataset-deep-dive.html` — BOE corpus, MITECO acts, lifecycle, compliance.
- `boa-dataset-deep-dive.html` — BOA corpus, INAGA + Dept. Industria, PDF format, SPA discovery.

These are authoritative for *what* we need to capture; the ADRs and the plan are authoritative for *how*.
