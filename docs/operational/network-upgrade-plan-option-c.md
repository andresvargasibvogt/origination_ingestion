# Network upgrade plan — Option A → Option C

- **Status:** Pre-positioned (not yet executed)
- **Date:** 2026-06-04
- **Owners:** Data platform team
- **Decision context:** [ADR-006 network posture](../decisions/0006-bo-ingest-network-posture-option-a.md) (current = Option A) · [ADR-002 network posture matrix](../decisions/0002-network-posture.md) · [ADR-007 compute + naming](../decisions/0007-compute-platform-confirmed-aca-jobs.md) · [ADR-008 staging pattern F](../decisions/0008-content-scanning-staging-pattern-f.md)

## Purpose

This document is the **execution plan** for upgrading the BOE ingest deployment from network posture **Option A (public-tier)** to **Option C (private VNet + workspace private link)**. It does not change the current decision (Option A stands per ADR-006). It exists so that **when a trigger fires** (Endesa onboarding with credentials, new Azure Policy, audit requirement, etc.), the upgrade is a one-day execution, not a one-day design + one-day execution.

Read order if you're acting on this: triggers → permissions checklist → architecture → migration steps → verification.

## Current state (Option A) — what we have today

```text
Subscription 53cc82cf-f636-425d-8e25-f37b6bb8ef8f (Azure SQL)
└─ rg-origination (northeurope)
   ├─ acrorigination          (ACR Basic, PUBLIC endpoint)
   ├─ log-origination         (Log Analytics)
   ├─ id-origination          (UAMI; SG-member; AcrPull on ACR)
   ├─ storiginationdmz        (Blob Storage, PUBLIC endpoint, Defender on-upload scan)
   │     untrusted/, quarantine/
   ├─ cae-origination         (ACA Environment, multi-tenant Consumption, PUBLIC)
   ├─ caj-boe-daily           (Schedule 0 7 * * 1-6)
   ├─ caj-boe-backfill        (Manual)
   └─ caj-promoter            (Schedule */5 * * * *  — promotes clean blobs → OneLake)
                              Writes to: lh_esp_origination via OneLake PUBLIC endpoint
```

Authentication: UAMI `id-origination` (principal `7058e3ff-2ddf-4f12-8db7-80e6688c84e3`) with `AcrPull` on ACR (via RG inheritance), `Storage Blob Data Contributor` on the staging account (pending grant), and Fabric workspace `Contributor` via membership in `Fabric - Central Members (DEV)`.

## Risks present today (Option A)

These are the gaps the upgrade closes. They are not theoretical — they're identified threat surfaces with concrete consequences.

| # | Risk | Likelihood | Impact today | Mitigation today |
|---|------|-----------|--------------|------------------|
| **R1** | A leaked MI access token (Entra OAuth, ~1 h TTL) can be replayed **from anywhere on the internet** to write to the staging account, push to ACR, or write to OneLake bronze. | Low (no credential leak yet) | High (attacker writes/deletes bronze; possibly poisons downstream extracts) | Identity-plane only: short token TTL + Defender for Cloud alerts on anomalous storage access. No network gate. |
| **R2** | `storiginationdmz` is reachable on its **public endpoint** by any client with credentials. | Low | Medium (unauthorized write/read) | Storage account firewall *could* be enabled with IP allowlist, but ACA multi-tenant pool has no static IP — so firewall would have to be disabled or wide-open. Currently no firewall config. |
| **R3** | ACR `acrorigination` is reachable on its **public endpoint** for both push and pull. | Low | Medium (image tampering) | Only the UAMI has AcrPull; only authorized identities have AcrPush. Identity-plane protection. |
| **R4** | The Fabric workspace `Central Data & Integration (DEV)` accepts traffic from any source on the internet. | Low | High (workspace contains data from multiple workloads, not just our loader) | Fabric workspace RBAC + Entra Conditional Access policies (subscription-wide, if configured). |
| **R5** | The ACA container's outbound egress is **unrestricted**: a compromised dependency can phone home anywhere. | Low (small dep set; hash-locked) | High (token exfil → R1 scenario plays out) | Hash-locked `uv.lock`, minimal base image, future Dependabot + ACR scanning (ADR-003 post-pilot items). No network layer. |
| **R6** | An **audit reviewing** the deployment finds public-tier compute writing to a shared analytics workspace and flags it. | Med (within 6–12 months as workspace grows) | High (mandatory remediation under deadline) | None today — we'd be reactive. |
| **R7** | When **Endesa credentials** enter scope (planned, this week or weeks ahead), Key Vault will be needed; KV's public endpoint is the same shape of R2. | Med (timeline-dependent) | Medium-High (creds in a public-endpoint store) | KV firewall + RBAC, similar caveats to R2. |

The current architecture is **defensible for public-domain BOE/BORME content with identity-only protection**. It becomes the wrong answer when the workspace accumulates non-public neighbours or credentials enter scope.

## Risks the upgrade mitigates

Direct mapping of each new resource to the risks it closes:

| New resource | Closes risks |
|--------------|--------------|
| Spoke VNet + subnet for ACA env (delegated `Microsoft.App/environments`) | R1, R5 — token leak useless from outside VNet; egress can be filtered via NSG/UDR if we ever add a hub firewall |
| Subnet for private endpoints (non-delegated) | Hosts R2/R3/R4/R7 endpoints below |
| NSG on each subnet | R5 — explicit allow/deny rules; default-deny inbound |
| ACA Environment recreated as **workload-profiles + internal-only** | R1 — env has no public IP; outbound through controlled path |
| Private endpoint to `acrorigination` (sub-resource `registry`) | R3 — ACR public endpoint can be disabled |
| Private endpoint to `storiginationdmz` (sub-resource `blob`) | R2 — staging account public endpoint can be disabled |
| Private endpoint to Fabric workspace (workspace PLS) | R4 — workspace inbound access protection forces all traffic through PE |
| Private endpoint to Key Vault (added when Endesa creds enter scope) | R7 — KV public endpoint can be disabled |
| Private DNS zone `privatelink.azurecr.io` + spoke VNet link | Resolves ACR FQDN to private IP |
| Private DNS zone `privatelink.blob.core.windows.net` + spoke VNet link | Same for blob |
| Private DNS zone `privatelink.dfs.fabric.microsoft.com` + spoke VNet link | Same for OneLake |
| Audit posture upgrade | R6 — VNet-injected workload is the baseline for most enterprise audits |

## Proposed architecture (Option C)

```text
Subscription 53cc82cf-f636-425d-8e25-f37b6bb8ef8f
└─ rg-origination (northeurope)
   │
   ├─ acrorigination                          (ACR Basic — but with Public network access DISABLED after upgrade)
   ├─ log-origination                         (Log Analytics)
   ├─ id-origination                          (UAMI — unchanged)
   ├─ storiginationdmz                        (Storage account — Public network access DISABLED after upgrade)
   │
   ├─ vnet-origination          [NEW]         /22 spoke VNet (e.g. 10.10.0.0/22)
   │  ├─ snet-aca               [NEW]         /24 subnet, delegated Microsoft.App/environments
   │  │  └─ nsg-aca             [NEW]         default-deny inbound, allow outbound to AzureCloud + boe.es
   │  └─ snet-pe                [NEW]         /26 subnet, no delegation
   │     └─ nsg-pe              [NEW]         default-deny inbound
   │
   ├─ cae-origination-v2        [REPLACES cae-origination]   ACA Environment, workload-profiles plan,
   │                                                          internal-only, injected into snet-aca
   │
   ├─ caj-boe-daily, caj-boe-backfill, caj-promoter   (Recreated in cae-origination-v2)
   │
   ├─ pe-acr-origination        [NEW]         Private endpoint → acrorigination
   ├─ pe-blob-origination       [NEW]         Private endpoint → storiginationdmz (blob sub-resource)
   ├─ pe-onelake-origination    [NEW]         Private endpoint → Fabric workspace (workspace PLS)
   │
   └─ Private DNS zones (linked to vnet-origination):
      ├─ privatelink.azurecr.io                            [NEW]
      ├─ privatelink.blob.core.windows.net                 [NEW]
      └─ privatelink.dfs.fabric.microsoft.com              [NEW]

External actions (not in our RG):
   ── Fabric tenant admin: enable "Inbound access protection" on workspace
      Central Data & Integration (DEV); approve the spoke's private endpoint
      from the Fabric workspace side.
```

After the upgrade:

- `acrorigination` and `storiginationdmz` both have **public network access = disabled**.
- The Fabric workspace's public endpoint **refuses traffic** unless it comes from an approved private endpoint.
- The ACA Environment has **no public IP**; outbound from it is via Microsoft backbone for Azure services (and the multi-tenant pool's outbound for `boe.es`).
- Our loader code is **unchanged**. The same `BlobWriter`, the same `OneLakeWriter` — they just resolve to private IPs via the new DNS zones.

## Permissions checklist (what the platform team must grant)

In addition to the two current asks (Storage Blob Data Contributor on staging + Defender for Storage enable), Option C needs:

| # | Permission / action | Granted to | Scope |
|---|--------------------|------------|-------|
| 1 | `Network Contributor` | the engineer running the upgrade | `rg-origination` (to create VNet / subnets / NSGs / PEs) |
| 2 | `Private DNS Zone Contributor` | the engineer running the upgrade | `rg-origination` (to create + link DNS zones) |
| 3 | `Contributor` on `acrorigination` | the engineer running the upgrade | The ACR resource (to disable public access + approve PE) |
| 4 | `Contributor` on `storiginationdmz` | the engineer running the upgrade | The storage account (to disable public access + approve PE) |
| 5 | **Fabric tenant admin action**: enable Inbound access protection on `Central Data & Integration (DEV)` workspace + approve PE | tenant admin | The Fabric workspace |
| 6 | Confirm Spain North / North Europe has **workload-profiles ACA environment + private endpoints** GA | self-verify | `az containerapp env workload-profile list-supported -l northeurope` |

Items 1–4 are typically granted to the workload owner ("Data platform team" identity / group) once. Item 5 is a per-environment Fabric admin handshake. Item 6 is a 30-second `az` query.

## Migration steps (when triggered)

### Pre-flight

1. Verify the BOE pilot has been running cleanly on Option A for ≥ 1 week. Make sure the daily and promoter Jobs are producing the expected manifests in OneLake. We don't want to debug a network issue and a code issue simultaneously.
2. Snapshot the current state: `az resource list -g rg-origination -o table` saved to `docs/operational/rg-origination-pre-upgrade.txt`.

### Phase 1 — Network plumbing (additive, non-destructive)

1. Create `vnet-origination` (10.10.0.0/22) + `snet-aca` (delegated Microsoft.App/environments) + `snet-pe`.
2. Create NSGs (`nsg-aca`, `nsg-pe`) with default-deny inbound and the usual allow rules.
3. Create the three Private DNS zones; link to `vnet-origination`.
4. Create the three private endpoints (ACR / storage / Fabric workspace). Approve PE on each target resource.
5. **Verify**: `nslookup acrorigination.azurecr.io` from a test container in the spoke returns a 10.10.x.x address.

### Phase 2 — ACA Environment recreation (destructive but contained)

ACA Environments cannot be converted from Consumption to workload-profiles in place. Required: delete + recreate.

6. Disable the schedule on `caj-boe-daily` and `caj-promoter` (set cron to `0 0 1 1 *` — yearly stub).
7. Delete `cae-origination` (this also deletes the three Jobs — they will be recreated).
8. Create `cae-origination-v2` as workload-profiles plan, VNet-injected into `snet-aca`, internal-only.
9. Recreate `caj-boe-daily`, `caj-boe-backfill`, `caj-promoter` in the new environment. Same images, same env vars, same args.
10. Restore the schedules.

Estimated downtime: ~30–60 minutes. With no production users today, this is academic.

### Phase 3 — Lock down (revoke public network access)

11. `az acr update -n acrorigination --public-network-enabled false`
12. `az storage account update -n storiginationdmz --public-network-access Disabled`
13. Fabric tenant admin enables Inbound access protection on the workspace.

### Phase 4 — Verify

14. Trigger `caj-boe-daily` manually. Confirm the full flow lands in OneLake (same as the Option A verification).
15. From outside the spoke VNet, attempt to read from `storiginationdmz` with a valid MI token. Expect denial (proving the network lock works).
16. Smoke-test the daily and promoter schedules on the next fire.

### Phase 5 — Document

17. Update [ADR-006](../decisions/0006-bo-ingest-network-posture-option-a.md): status → "Superseded by Option C (executed YYYY-MM-DD)." Add the migration date and trigger that fired.

## Cost estimate

| Item | Recurring | One-time |
|------|-----------|----------|
| Workload-profiles ACA Environment baseline | ~€80/mo | — |
| 3 × private endpoints @ ~€10/mo | ~€30/mo | — |
| Private DNS zones | €0 (DNS zones are free; DNS query charges negligible at our volume) | — |
| Engineer time | — | ~1 day |
| Fabric admin handshake | — | ~30 min |
| Total | **~€110/mo** | **~1 day** |

Cost relative to Option A: **~€110/mo more** (mostly the workload-profiles plan baseline; the PE fees are small).

## Triggers to execute this plan

Any one of these should pull the trigger:

1. **Endesa or any other source enters scope with credentials.** Credentials in Option A's identity-only model is the most-likely real trigger.
2. **Azure Policy denying public-network resources** lands in the subscription.
3. **Audit feedback** requires VNet-injection for the workload.
4. **The Fabric workspace `Central Data & Integration (DEV)`** accumulates non-public data alongside our public BOE corpus.
5. **A real incident** (leaked MI token, malicious upload not caught by Defender, etc.).

## Alternative path: replace the entire pipeline with Apify

A reasonable steelman keeps coming up: *instead of running our own VNet-injected agent pools, why not just let an Apify Actor do the scraping?* This section takes that seriously and lays out where it works, where it doesn't, and what posture it commits us to.

### What Apify Enterprise offers (researched 2026-06-04)

Sourced from [apify.com/pricing](https://apify.com/pricing), [apify.com/enterprise](https://apify.com/enterprise), and the Apify SOC 2 blog post:

- **Compliance**: SOC 2 Type II (Security / Availability / Confidentiality, audited by Prescient Security), GDPR, CCPA. 99.95% uptime claim.
- **Identity & access**: SSO / SAML, team management, RBAC.
- **Commercial**: custom SLA, account manager, dedicated proxies, custom credit pools, $0.13 / compute-unit, 256 GB actor RAM, 256 concurrent runs.
- **Deployment model**: SaaS only. Multi-tenant Apify cloud (hosted on AWS). **No published** support for VPC peering, customer-VNet deployment, Azure Private Link, BYOC, on-premise install, customer-managed encryption keys, or EU-pinned data residency. Available "if you contact sales" — i.e. not a documented product feature.

### The step the framing skips: data still has to land in OneLake

"Apify does the job" sounds clean, but the Actor finishing ≠ data in OneLake. Some workload has to hold OneLake write credentials and execute the write. Two concrete shapes:

#### Architecture A — Apify writes directly to OneLake

```text
Apify Cloud (AWS, multi-tenant)
   Actor: BOE-ingest
        │
        │ ABFS over public internet
        │ auth: client_credentials (Entra SP, secret stored in Apify Secrets)
        ▼
Fabric Workspace "Central Data & Integration (DEV)" (public endpoint)
   lh_esp_origination / Files/bronze/boe/raw/...
```

- We create an Entra service principal, grant it workspace Contributor, store its client secret in Apify's Secrets store.
- OneLake **must accept public traffic** from Apify's egress IPs (no stable list published).
- No ACR, no UAMI, no Pattern F staging blob, no promoter, no ACA Env.

#### Architecture B — Apify stages to its own store; our puller reconciles into OneLake

```text
Apify Cloud                              Our Azure tenant
   Actor: BOE-ingest                       caj-apify-puller (ACA Job)
        │                                       │
        │  writes to                            │ pulls via Apify API
        ▼                                       ▼
   Dataset / Key-Value Store ────────────► OneLake bronze
                                           via UAMI (same as today)
```

- We keep a small workload of our own. The puller is dumb — no parsing, no anti-bot — just "GET from Apify, write to OneLake."
- All the Pattern F apparatus (staging blob, Defender, promoter, UAMI) can stay or simplify.
- Two systems instead of one; bill paid to both.

### Where this wins

Honest case for the Apify path (especially Architecture A):

1. **Zero Azure ingestion infra to manage.** No ACA Env baseline, no ACR, no UAMI, no staging account, no promoter. Real cost+complexity win for a team that doesn't want to be an Azure ops team.
2. **Apify sandboxes the Actor.** Their multi-tenant isolation is the core security product. A compromised Actor doesn't get lateral reach into our tenant — it gets the OneLake credentials, scoped to one workspace, and nothing else.
3. **Anti-bot / proxies / JS rendering are free for future sources.** For BOE we don't need them; for Endesa we probably will. Consolidating both on Apify is one operational surface instead of two.
4. **The pattern is enterprise-precedented.** Fivetran, Stitch, Airbyte Cloud, Hevo all hold service principals that write to customer lakehouses. Apify in this mode is structurally identical — it's not a weird ask of a security team.
5. **Time-to-source.** A generic "PDF downloader" Actor from the Apify Store plus configuration is plausibly a half-day, vs ~3 days for our ACA pipeline. (Trade-off: less customisation control.)

### Where it loses for BOE today

1. **For BOE specifically, you pay for capabilities you don't use.** Apify's value lives in anti-bot / proxies / JS / captcha. BOE is `httpx.get(url).content` on a public open-data API. Bugatti to the parking lot.
2. **The OneLake credentials problem gets *worse*, not better.** Today, a leaked UAMI token has ~1-hour TTL and is scoped to the staging storage account. In Architecture A, a leaked Apify-stored client secret is valid for months (Entra default), grants workspace-wide Contributor, and the blast radius is governed by Apify's secret-store posture rather than ours. **Trades short-lived narrow MI for long-lived broad vendor-held secret. Downgrade for R1/R7.**
3. **Audit surface expands.** Apify SOC 2 report, DPA, sub-processor list, GDPR Article 28 controller/processor agreement, AWS-residency disclosure (their substrate), incident-response RACI between us and them. More paperwork than running our own ACA Job. **Worsens R6.**
4. **Architecture A forecloses Option C.** The whole point of this upgrade is to put the Fabric workspace behind inbound private link. If Apify writes to it from their AWS cloud, you can never enable that — Apify doesn't publish stable egress IPs and offers no Azure Private Link path. **Choosing Apify for BOE permanently closes off Option C for the workspace.** That's a long-term cost.
5. **Lock-in.** Our current loader is ~150 lines of `httpx` + Pydantic — rsync the directory and it runs anywhere. An Apify Actor is coupled to their Crawlee SDK, Actor manifest, and input/output schemas. Migration cost goes from "trivial" to "rewrite."
6. **Recurring spend.** BOE volume is small (~200 PDFs/day × 25 days/month), but Apify CU usage is real and recurring. Probably €10–30/mo. Current ACA Consumption usage on Option A is functionally free at this volume. Not the decider; not zero.
7. **BOE is the wrong source to test the pattern.** A successful Apify run on BOE proves Apify can do what 150 lines of Python can already do. It doesn't validate Apify on what it's *for*. The right place to evaluate Apify is Endesa, where anti-bot/JS make Apify's value proposition real.

### The choice underneath the choice

Architecture A and Option C are **incompatible postures**, not alternative implementations:

- **Option C posture**: "The Fabric workspace boundary matters. Writes happen from inside our tenant. No third-party SaaS holds workspace write credentials."
- **Apify Architecture A posture**: "OneLake stays publicly reachable forever. We accept a vendor SP as a workspace Contributor. The lakehouse boundary is the SP credential, not the network."

Either is defensible. Pick one. The question isn't *"ACA vs Apify"* — it's *"are we OK with a third-party SaaS holding lakehouse write credentials forever?"* That's a CISO/policy question, not a technical question.

Architecture B (Apify stages, we pull) sidesteps the credentials question but **doesn't eliminate the agent-pool work** — we still run an ACA Job, just one that reads from Apify instead of from BOE. It's strictly more moving parts than the status quo for BOE.

### Decision for this document

- **BOE stays on ACA.** Apify's strengths don't apply, and the credentials/audit trade is a net negative for a public-API source. ADR-006 still stands.
- **Option C remains the right network upgrade plan** when its triggers fire. It is incompatible with Apify Architecture A; teams considering both should treat them as a posture choice, not stacked layers.
- **Endesa onboarding gets its own ADR.** That's where the Apify question is genuinely live, and the ADR will need to resolve the OneLake-write architecture (A vs B vs our own ACA Job) explicitly — not punt on it.

Reference: ADR-001 §"Apify (maximalist mode) rejected" and ADR-004 §"Pattern C2: Apify Hosted (Narrow)" already cover the per-source Apify decision tree. This document only addresses whether Apify changes the *network upgrade* calculus — and the answer is that it changes the **posture**, not the upgrade itself.

## What this plan does **not** cover

- **Option D** (hub-peering + Azure Firewall + L7 egress inspection). That's a separate upgrade from C. Required only if a downstream consumer needs static egress IPs or the security team requires deep egress inspection. ADR-002 documents that level.
- **Multi-region BC/DR.** This plan keeps the deployment single-region (North Europe). DR would add a secondary VNet in another region; out of scope.
- **Per-source UAMI carve-out.** Orthogonal to the network upgrade. Documented as a future item in ADR-007.
- **Replacing Pattern F with native OneLake scanning.** Not currently possible (OneLake doesn't support Defender for Storage). Revisit when Microsoft adds it.
- **Per-source Apify evaluation.** Covered above as an alternative; full per-source decision belongs in a future ADR (likely triggered by Endesa onboarding).

## Reversibility

Option C → Option A rollback is **destructive again** — recreate the ACA Environment as Consumption multi-tenant, delete the network resources, re-enable public network access on ACR and storage. ~1 day. Easier than the A → C upgrade because there's no Fabric admin handshake to coordinate the reverse direction (just disable Inbound access protection in Fabric).

We would only roll back if Option C proved more expensive than the team is willing to pay AND no trigger requires it. Unlikely but documented for completeness.
