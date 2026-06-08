# ADR-007 — Compute platform + naming + single-deployment structure

- **Status:** Accepted
- **Date:** 2026-06-02 (revised same day for naming + single-env structure)
- **Owners:** Data platform team
- **Confirms:** [ADR-001](0001-compute-platform.md) in light of the multi-source growth plan
- **Related:** [ADR-003 supply-chain](0003-supply-chain-controls.md), [ADR-005 OneLake folder structure](0005-onelake-folder-structure.md), [ADR-006 network posture](0006-bo-ingest-network-posture-option-a.md)

## Three decisions captured here

1. **Compute platform**: Azure Container Apps Jobs — *confirmed*, not switched to Functions, because the growth scenario favours containers.
2. **Naming**: a CAF-aligned standard with **`origination`** as the workload domain (mirrors the lakehouse name `lh_esp_origination`).
3. **Deployment structure**: a single Azure deployment (no dev/stg/prod split for the platform resources). The target Fabric workspace is parameterised via env var — first deploy targets `Central Data & Integration (DEV)`.

## Why ACA Jobs (re-evaluated against the multi-source growth)

[ADR-001](0001-compute-platform.md) chose ACA Jobs over Azure Functions before the loader was built. After measuring it — ~3 second daily runs, 150 MB container, no fan-out — Functions Consumption would handle BOE alone trivially. So the choice came up for re-evaluation.

The decision flipped to "stay with ACA Jobs" once we mapped the realistic 12-month roadmap:

| Source | Access shape | Container needs | Runtime |
|--------|-------------|-----------------|---------|
| **BOE** (today) | Clean JSON API + PDFs | `python:3.12-slim`, ~150 MB | ~3 sec/day |
| **BOA** (next) | Angular SPA + BRSCGI PDFs — **needs Chromium / Playwright** | `mcr.microsoft.com/playwright/python`, ~700 MB | 1–2 min/day |
| **Endesa** | Login-protected portal, XLSX — likely **needs Selenium-grade automation** | Custom, ~500–800 MB | 30 s–2 min/day |
| **REE** | Possibly clean API, possibly XLSX via portal | Varies | 10–30 s/day |
| **MITECO dossiers** | Large ZIPs from ephemeral `almacen.redsara.es` URLs | Slim Python | Variable, can spike |
| **BORME** | Same shape as BOE | `python:3.12-slim` | ~3 sec/day |

Heterogeneous runtimes. Functions Consumption can't run BOA's Playwright stack. Functions Flex Consumption requires custom containers with the Functions base image (~700 MB) — at which point the simplicity argument disappears.

**ACA Jobs gives uniform deployment for all sources**: each source is a container, each gets its own ACA Job, shared environment / identity / observability. Adding a new source = build container + add Job. No platform switch.

## Naming standard

Format: `{azure-prefix}-{domain}-{purpose-or-source}`

- **`{azure-prefix}`**: standard 2–4 letter Azure resource type tag (`rg`, `id`, `cae`, `caj`, `ag`, `pe`, etc.).
- **`{domain}`**: **`origination`** — the data domain identifier. Mirrors the Fabric lakehouse `lh_esp_origination` (lakehouse uses underscore; Azure requires hyphen; same domain).
- **`{purpose-or-source}`**: only used for resources that vary per source or per role. Sources: `boe`, `boa`, `endesa`, `ree`, `borme`, `miteco-dossiers`, etc. Roles: `daily`, `backfill`.

No environment token in names — single deployment for now. If multi-env separation becomes necessary, suffix `-dev` / `-stg` / `-prod` will be added in a future ADR.

No region suffix — single-region (Spain Central). Add `-spc` later if multi-region is on the roadmap.

### Concrete resource names

| Resource | Name | Rationale |
|----------|------|-----------|
| Resource Group | `rg-origination` | Holds everything for the data-origination domain |
| User-Assigned Managed Identity | `id-origination` | One identity, shared across all sources |
| ACA Environment | `cae-origination` | One environment, all source Jobs live here |
| ACA Job — BOE daily | `caj-boe-daily` | Per source + role |
| ACA Job — BOE backfill | `caj-boe-backfill` | Same |
| ACA Job — BOA daily (future) | `caj-boa-daily` | Same pattern |
| ACA Job — Endesa daily (future) | `caj-endesa-daily` | Same |
| Action Group (optional) | `ag-origination` | One group, fires for any Job failure |
| Diagnostic settings on each Job | `diag-{job-name}` | Per Job, sends to existing Log Analytics |
| Container images in existing ACR | `{acr}/boe-ingest:{tag}`, `{acr}/boa-ingest:{tag}`, etc. | One image per source |

## Region — under decision

**Fixed constraint**: the Fabric capacity backing `Central Data & Integration (DEV|STG|PROD)` is in **North Europe (Dublin)**. The OneLake storage for `lh_esp_origination` lives there regardless of where we put the ACA Job.

We are choosing the **ACA Job's region**, independently of (but in light of) where the data lake lives.

### Candidates considered

| | Spain Central | North Europe (Dublin) | West Europe (Amsterdam) |
|--|---------------|----------------------|--------------------------|
| RTT to `api.boe.es` (source) | ~5 ms | ~40 ms | ~30 ms |
| RTT to OneLake (Dublin, fixed) | ~40 ms (**cross-region**) | <5 ms (**same region**) | ~10 ms (cross-region) |
| Inter-region egress charges (at our 1 MB/day write volume) | ~$0.007/year (trivial) | $0 | ~$0.007/year (trivial) |
| Same outage zone as storage | No | **Yes** | No |
| Feature parity (ACA Jobs Consumption, MI, diagnostic settings) | GA | GA | GA |
| Microsoft regional pairing (BC/DR) | Spain Central (self-paired today) | Ireland ↔ Netherlands | Netherlands ↔ Ireland |
| Symbolic alignment with Spanish public-sector data | **Strong** | None | None |
| Symbolic alignment with the team's existing EU compute footprint | (depends on the team's pattern) | (depends) | (depends) |
| Time-zone alignment with operators (if team is Spain-based) | **Match** | +0–1 h | +0–1 h |

### What each factor is actually worth, at this workload

- **Latency**: irrelevant. A 35 ms difference per round-trip × 5 PDFs/day = ~175 ms total, vs. the 3 s the whole run takes. Not a deciding factor.
- **Cost (egress)**: irrelevant. $0.007/year doesn't move any decision.
- **Outage coupling**: **real, modest weight.** Same-region compute + storage means one outage zone — simpler failure modes. Cross-region adds a second outage zone and a backbone-transit hop.
- **Data sovereignty / symbolism**: **real, depends on the company's positioning.** "Spanish public-sector data processed in Spain" is a coherent story end-to-end *only if* the storage is also in Spain — which it isn't here. The chain breaks at Dublin regardless.
- **Operational consistency**: **real.** If the platform team already concentrates EU workloads in North Europe (or in Spain Central), the region choice should align — adding a new region to an enterprise estate has real maintenance cost beyond this one workload.

### Honest recommendation

**North Europe**, because the load-bearing fact is that the data layer is already there. Putting compute in Spain Central buys the symbolic alignment but pays for it with cross-region transit complexity that we get nothing concrete from at this volume. Putting compute in West Europe is strictly dominated — same cross-region penalty, no offsetting benefit.

I'd hold this recommendation **loosely** — three reasons could flip it:

1. **The platform team already standardises on Spain Central** for all workloads in this tenant. Region consistency across the estate is worth more than a few ms.
2. **A Spanish public-sector contract or compliance commitment** that names "Spain-region processing" specifically. Symbolic narrative becomes a contractual requirement.
3. **The team operates from Spain and wants resources in their time zone** for support workflows (logs, incident response).

### Triggers to revisit the region choice (whichever we pick)

1. The Fabric capacity moves to a different region. Re-co-locate compute there.
2. A platform policy mandates a specific region tenant-wide.
3. Multi-region BC/DR becomes a requirement. Add a secondary; primary stays where storage is.

### Region migration cost (if we ever need to move)

Region migration is **not in-place** for ACA resources. To move:

1. Provision a new RG in the target region with the same naming (`rg-origination`).
2. Re-run the deploy script with the new `LOC` value.
3. Cut over: update the Job env vars (Fabric workspace target is region-independent; the UAMI's group membership doesn't care about region).
4. Decommission the old RG.

Estimated effort: ~half a day. Mechanical, because no state lives in compute — bronze in OneLake is the only state, and it doesn't move when compute moves.

### Decision

**`northeurope`** (Dublin). Chosen on 2026-06-02 to co-locate ACA with the Fabric capacity that hosts `lh_esp_origination`. Same-region compute + storage = one outage zone, zero cross-region transit, lowest write RTT to OneLake. The "Spanish data in Spain" symbolism argument was considered and rejected — the storage being in Dublin already breaks that narrative.

## Deployment structure (single Azure deployment, multiple Fabric workspaces over time)

We provision the Azure platform **once**. The target Fabric workspace is decided at runtime via the `FABRIC_WORKSPACE_NAME` env var on each Job.

```text
Single Azure deployment (in subscription / region):
  rg-origination
    ├─ id-origination
    ├─ cae-origination
    │     ├─ caj-boe-daily              FABRIC_WORKSPACE_NAME = "Central Data & Integration (DEV)"  ← first deploy
    │     ├─ caj-boe-backfill           FABRIC_WORKSPACE_NAME = "Central Data & Integration (DEV)"
    │     ├─ caj-boa-daily              (future)
    │     ├─ caj-endesa-daily           (future)
    │     └─ ...
    └─ ag-origination (optional)
```

Promoting from DEV to STG/PROD later = update one env var on the Jobs (`az containerapp job update --set-env-vars FABRIC_WORKSPACE_NAME=...`) or roll out a fresh deployment if dev/stg/prod isolation becomes required.

### Why single deployment (vs three from day one)

- The team's preference: keep the initial footprint minimal.
- The compute is identical across DEV/STG/PROD; only the Fabric workspace target changes — which is a per-Job env var, not a per-resource property.
- The platform team can split it later if compliance or change-management policy requires it; the migration is mechanical (recreate the RG with `-dev` suffix, repeat for `-stg` / `-prod`).

## Identity pattern — UAMI joins existing security group

We do **not** grant the UAMI Fabric workspace `Contributor` directly. Instead:

```text
Existing Entra security group (already has Fabric Contributor on the target workspace)
   │   ↑ pre-existing platform-team configuration; we do not modify the grant
   │
   └─ NEW: we add id-origination as a member of this group
              ↓
       UAMI inherits Contributor via group membership
       → writes Files/bronze/{source}/raw/... on its behalf
```

**Why**:

- No Fabric admin handshake required per source. The group has the role; adding new identities is an Entra admin action (`az ad group member add`).
- Permissions follow the existing organisational pattern — if the team rotates the group's grant, the UAMI follows automatically.
- Same UAMI serves all sources. Each source's container writes to its own `Files/bronze/{source}/raw/` path, isolated by code, not by RBAC.

If a future source requires stronger isolation (e.g. PII handling), a per-source UAMI can be created at that point — additive, not breaking.

## Services we need to provision — exhaustive list

For one Azure deployment targeting Fabric DEV first:

| # | Resource | Type | Suggested name | Notes |
|---|----------|------|----------------|-------|
| 1 | Resource Group *(already exists)* | `Microsoft.Resources/resourceGroups` | `rg-origination` | Region: `northeurope` (co-located with Fabric capacity). Created manually 2026-06-02. |
| 2 | User-Assigned Managed Identity | `Microsoft.ManagedIdentity/userAssignedIdentities` | `id-origination` | Shared across all source Jobs |
| 3 | **Azure Container Registry** *(new — see "Why dedicated ACR" below)* | `Microsoft.ContainerRegistry/registries` | `acrorigination` (or generated suffix if name is taken) | Basic SKU. Holds all source images. |
| 4 | **Log Analytics workspace** *(new — see "Why dedicated LA" below)* | `Microsoft.OperationalInsights/workspaces` | `log-origination` | PerGB2018 SKU. Scoped to ingestion logs only. |
| 5 | ACA Environment | `Microsoft.App/managedEnvironments` | `cae-origination` | Consumption multi-tenant (Option A per ADR-006); logs to the new `log-origination` |
| 6 | ACA Job — BOE daily | `Microsoft.App/jobs` | `caj-boe-daily` | CRON `0 7 * * 1-6`, args `--date today` |
| 7 | ACA Job — BOE backfill | `Microsoft.App/jobs` | `caj-boe-backfill` | Manual trigger, args overridden per run |
| 8 | Role assignment: UAMI → AcrPull on the new ACR | `Microsoft.Authorization/roleAssignments` | (auto-named) | Pull images for the Jobs |
| 9 | Entra group membership: UAMI joins existing security group | Microsoft Entra | — | `az ad group member add` (the SG already has Fabric Contributor) |
| 10 | Diagnostic settings on each Job | `Microsoft.Insights/diagnosticSettings` | `diag-{job-name}` | Categories: ContainerApp* logs → `log-origination` |
| 11 | Action Group (optional) | `Microsoft.Insights/actionGroups` | `ag-origination` | For job-failure alerts |
| 12 | Alert rule (optional) | `Microsoft.Insights/scheduledQueryRules` | `alert-job-failed-origination` | KQL alert on Job failure events |
| 13 | Container image | image artifact | `acrorigination.azurecr.io/boe-ingest:{tag}` | Built via `az acr build`, pushed to the new ACR |

Existing resources we reference (do not provision):

- Azure subscription `53cc82cf-f636-425d-8e25-f37b6bb8ef8f` (`Azure SQL`)
- Fabric workspace `Central Data & Integration (DEV)` (target — confirmed)
- Lakehouse `lh_esp_origination` (target — confirmed)
- Microsoft Entra tenant `2019dd21-44d9-4bf8-8b6e-aeeba882c8b9` (confirmed)
- Entra security groups with Fabric Contributor on the corresponding workspace (resolved 2026-06-03):

  | Env | Display name | Object ID |
  |-----|--------------|-----------|
  | DEV  | `Fabric - Central Members (DEV)`  | `a478fcaa-0a99-4c05-aaa1-640f8d2ef5dc` |
  | STG  | `Fabric - Central Members (STG)`  | `da7c8106-b8b2-4461-a2ab-373901d60106` |
  | PROD | `Fabric - Central Members (PROD)` | `a101d1ca-cb13-473f-bdb8-928b8040d9ae` |

  The DEV group is wired into the deploy script as the default; STG/PROD object IDs are recorded here for when those deployments happen (override via `CONTRIBUTOR_GROUP_OBJECT_ID` env var).

### Why dedicated ACR (`acrorigination`)

Subscription enumeration found only one existing registry: `autolayoutdevengacr` (Basic, in `germanywestcentral`, in `rg_developmenteng`). Two reasons we provision a fresh one rather than share it:

1. **Scope and ownership.** That registry belongs to a different project (Autolayout / development eng). Sharing it would couple our image lifecycle to theirs — their cleanup policies, their access controls, their cost charge-back. Dedicated keeps ownership clean.
2. **Region.** Their ACR is in Germany West Central; our ACA is in North Europe. Cross-region image pulls work but add small egress cost and ~10–30 ms cold-start latency per pull. A new ACR in North Europe co-locates with the Jobs.

**Trade-offs accepted**: ~€5/mo for Basic SKU (the smallest billable unit). No private endpoints (Basic SKU; matches Option A). Single point of failure for image pulls — acceptable at our scale; if it becomes a constraint we can promote to Standard/Premium and add geo-replication (one-line change).

### Why dedicated Log Analytics (`log-origination`)

Subscription enumeration found four LA workspaces, none clearly purposed for ingestion workloads:

- Three `DefaultWorkspace-*` workspaces — auto-generated by Microsoft for default Defender / Security Center landing. Mixed-use; logs from many sources would be intermingled.
- One project-scoped `workspace-rgdevelopmentengzaTJ` — belongs to another project, in Germany West Central.

Two reasons we provision a fresh one:

1. **Log scoping.** Mixing ingestion logs with Defender / Security Center logs makes the loader's logs hard to find. A dedicated workspace gives clean KQL queries scoped to *just* our Jobs.
2. **Region.** New workspace lives in North Europe alongside the ACA Environment — same region, no cross-region log shipping.

**Trade-offs accepted**: log volume is tiny (~5 MB/day) — well under the 5 GB/month free tier of `PerGB2018`. Effective cost: €0 at our volume. Migration cost if we ever want to consolidate into an enterprise workspace: trivial (point diagnostic settings at a different workspace ID; existing logs stay where they were).

## Provisioning checklist (az CLI) — adjusted to the new naming

```bash
# ─── Variables (subscription + RG already exist) ─────────────────────────
SUB=53cc82cf-f636-425d-8e25-f37b6bb8ef8f
RG=rg-origination              # already created manually 2026-06-02
LOC=northeurope                # co-located with the Fabric capacity (see Region section)

ACR_NAME=acrorigination        # globally unique — append numeric suffix if name is taken
LAW=log-origination
UAMI=id-origination
ACA_ENV=cae-origination
JOB_DAILY=caj-boe-daily
JOB_BACKFILL=caj-boe-backfill

WORKSPACE_NAME="Central Data & Integration (DEV)"   # confirmed target Fabric workspace
CONTRIBUTOR_GROUP_OBJECT_ID=                         # existing Entra SG with Fabric Contributor

az account set --subscription $SUB

# ─── 1. Azure Container Registry (NEW, dedicated — see "Why dedicated ACR") ──
az acr create -n $ACR_NAME -g $RG -l $LOC --sku Basic --admin-enabled false
ACR_ID=$(az acr show -n $ACR_NAME -g $RG --query id -o tsv)

# ─── 2. Log Analytics workspace (NEW, dedicated — see "Why dedicated LA") ────
az monitor log-analytics workspace create -n $LAW -g $RG -l $LOC --sku PerGB2018
LAW_ID=$(az monitor log-analytics workspace show -n $LAW -g $RG --query id -o tsv)
LAW_CUSTOMER_ID=$(az monitor log-analytics workspace show -n $LAW -g $RG --query customerId -o tsv)
LAW_SHARED_KEY=$(az monitor log-analytics workspace get-shared-keys -n $LAW -g $RG --query primarySharedKey -o tsv)

# ─── 3. User-assigned managed identity ───────────────────────────────────
az identity create -n $UAMI -g $RG -l $LOC
UAMI_ID=$(az identity show -n $UAMI -g $RG --query id -o tsv)
UAMI_CLIENT_ID=$(az identity show -n $UAMI -g $RG --query clientId -o tsv)
UAMI_PRINCIPAL_ID=$(az identity show -n $UAMI -g $RG --query principalId -o tsv)

# ─── 4. Grant AcrPull on the new ACR ─────────────────────────────────────
az role assignment create \
  --assignee-object-id $UAMI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role AcrPull \
  --scope $ACR_ID

# ─── 5. Add UAMI to the existing Entra security group ────────────────────
# The SG already has Fabric Contributor on "Central Data & Integration (DEV)".
# By adding the UAMI as a member, it inherits that role.
az ad group member add \
  --group $CONTRIBUTOR_GROUP_OBJECT_ID \
  --member-id $UAMI_PRINCIPAL_ID

# ─── 6. Build + push the image to the new ACR ────────────────────────────
az acr build -r $ACR_NAME -t boe-ingest:latest -f containers/boe.Dockerfile .
IMAGE_TAG=$ACR_NAME.azurecr.io/boe-ingest:latest

# ─── 7. ACA Environment (Consumption, multi-tenant per ADR-006) ──────────
az containerapp env create \
  -n $ACA_ENV -g $RG -l $LOC \
  --logs-destination log-analytics \
  --logs-workspace-id $LAW_CUSTOMER_ID \
  --logs-workspace-key $LAW_SHARED_KEY

# ─── 8. ACA Job — BOE daily, CRON 07:00 UTC Mon-Sat ──────────────────────
az containerapp job create \
  -n $JOB_DAILY -g $RG \
  --environment $ACA_ENV \
  --trigger-type Schedule \
  --cron-expression "0 7 * * 1-6" \
  --replica-timeout 1800 \
  --replica-retry-limit 1 \
  --parallelism 1 \
  --replica-completion-count 1 \
  --image $IMAGE_TAG \
  --cpu 0.5 --memory 1Gi \
  --mi-user-assigned $UAMI_ID \
  --registry-server $ACR_NAME.azurecr.io \
  --registry-identity $UAMI_ID \
  --env-vars \
      FABRIC_WORKSPACE_NAME="$WORKSPACE_NAME" \
      FABRIC_LAKEHOUSE_NAME="lh_esp_origination" \
      AZURE_CLIENT_ID="$UAMI_CLIENT_ID" \
      BOE_USER_AGENT="boe-ingest/1.0" \
  --args "--date" "today"

# ─── 9. ACA Job — BOE backfill (manual trigger) ──────────────────────────
az containerapp job create \
  -n $JOB_BACKFILL -g $RG \
  --environment $ACA_ENV \
  --trigger-type Manual \
  --replica-timeout 3600 \
  --replica-retry-limit 1 \
  --image $IMAGE_TAG \
  --cpu 0.5 --memory 1Gi \
  --mi-user-assigned $UAMI_ID \
  --registry-server $ACR_NAME.azurecr.io \
  --registry-identity $UAMI_ID \
  --env-vars \
      FABRIC_WORKSPACE_NAME="$WORKSPACE_NAME" \
      FABRIC_LAKEHOUSE_NAME="lh_esp_origination" \
      AZURE_CLIENT_ID="$UAMI_CLIENT_ID" \
      BOE_USER_AGENT="boe-ingest/1.0"

# ─── 10. Verify ──────────────────────────────────────────────────────────
az containerapp job start -n $JOB_DAILY -g $RG
az containerapp job execution list -n $JOB_DAILY -g $RG -o table
```

## Adding a new source later

The platform resources (RG, UAMI, ACA env) already exist. Adding a new source is just:

1. Build + push that source's container image: `az acr build -r $ACR_NAME -t boa-ingest:latest -f containers/boa.Dockerfile .`
2. Create the new Job(s):

   ```bash
   az containerapp job create \
     -n caj-boa-daily -g $RG \
     --environment $ACA_ENV \
     --image $ACR_NAME.azurecr.io/boa-ingest:latest \
     --trigger-type Schedule \
     --cron-expression "0 8 * * 1-5" \
     ... (same MI, same env vars + source-specific args)
   ```

No new RG, no new UAMI, no new RBAC handshake.

## Triggers to revisit

1. **dev/stg/prod separation becomes mandatory** (compliance, change management) → split into `rg-origination-{env}` per env.
2. **A source requires different RBAC** than the shared UAMI provides → carve out a per-source UAMI.
3. **The "growth scenario" reverses** — only BOE forever, no other sources → reconsider Functions Consumption for simplicity.
4. **A platform policy** mandates network isolation → revisit ADR-006 (Option A → C).

## Reversibility

- Renaming resources: **medium effort** (Azure resources can't be renamed in place; would need recreate + cutover; ~half-day per env).
- Splitting single-env into dev/stg/prod: **low effort** — duplicate the deploy script per env, run three times.
- Splitting shared UAMI into per-source UAMIs: **low effort** — create new UAMI, add to group, update one Job.
