# ADR-006 — Network posture for the BOE loader: Option A (public-tier)

- **Status:** Accepted
- **Date:** 2026-06-02
- **Owners:** Data platform team
- **Supersedes (for this workload only):** [ADR-002](0002-network-posture.md) default of Option C
- **Related:** [ADR-001 compute platform](0001-compute-platform.md), [ADR-005 OneLake folder structure](0005-onelake-folder-structure.md)

## Context

[ADR-002](0002-network-posture.md) compared four network postures for *any* loader writing to OneLake, and named Option C (private VNet + workspace private link) as the default for enterprise prod workloads. That ADR was deliberately written as a general policy.

This ADR records the **specific deployment choice for the BOE ingest loader**. After building the loader and verifying its behaviour against live BOE data, we made the case for downgrading to Option A (public-tier) for this workload while leaving ADR-002 standing as the general default.

## What the loader actually does (verified 2026-06-02)

End-to-end traffic profile:

| Direction | Endpoint | Purpose | Frequency |
|-----------|----------|---------|-----------|
| Outbound | `api.boe.es` (one host, public, no auth) | Fetch daily sumario JSON | Once/day |
| Outbound | `boe.es/boe/dias/.../pdfs/*.pdf` (same host family) | Fetch matched PDFs | ~3–10/day |
| Outbound | `boe.es/robots.txt` | Compliance check | Once/run |
| Outbound | Container registry (existing ACR) | Image pull on cold-start | Once/run |
| Outbound | `onelake.dfs.fabric.microsoft.com` | Write PDFs + manifest to bronze | Per item |
| Outbound | `login.microsoftonline.com` / `*.entra.microsoftonline.com` | Token acquisition for the MI | Per run |
| Inbound | None | ACA Jobs have no listeners | — |

Identity surface:

| Identity | Scope of access | If leaked |
|----------|----------------|-----------|
| User-assigned MI for the loader | `AcrPull` on the specific ACR (image read only) + `Contributor` on the specific Fabric workspace (write to bronze) | Token TTL ~1 hour. Could write/delete `Files/bronze/boe/raw/...`. Could be revoked in seconds via Entra. |

Data confidentiality:

- Source content: **public-domain Spanish state gazette PDFs**. Zero confidentiality.
- Sink content (bronze): identical bytes to source. Zero confidentiality of *this corpus*.
- Adjacent workspace content: may include sensitive non-BOE data; **this is the only real attack surface argument for network-layer protection.**

## Decision

**Adopt Option A** (public-tier ACA environment, public OneLake endpoint, MI authentication only) **for the BOE loader**.

Concretely:

- ACA environment: **multi-tenant Consumption plan**, no VNet integration.
- OneLake access: via the **public endpoint** `onelake.dfs.fabric.microsoft.com`. No workspace private link is required from our side.
- ACR access: via the registry's **public endpoint**. No private endpoint required.
- Auth: user-assigned MI → Entra → OAuth token → OneLake / ACR / Entra. Standard pattern.

Terraform consequence: no `network` module, no `private-endpoints` module, no Private DNS zones — about 3 files of Terraform instead of 6, ~80 lines instead of ~250.

## Reasoning

Four arguments, in priority order:

1. **The data is fully public-domain.** Spanish PSI regime explicitly permits redistribution of BOE content with attribution. There is no confidentiality dimension to defend on the source side.

2. **The MI's blast radius is tightly scoped.** The only RBAC the MI carries is `AcrPull` on one specific ACR and `Contributor` on one specific Fabric workspace. A leaked token cannot reach other workspaces, other lakehouses, or other Azure subscriptions.

3. **Token revocation is fast.** If a credential is compromised, an Entra admin can rotate the UAMI's credential or remove the workspace role assignment in under a minute. Defence-in-depth via network is valuable when revocation is slow; here, it's not.

4. **First deploy is hours, not days.** Option C requires a Fabric tenant admin to enable workspace inbound private link, a spoke VNet design that may need hub peering, and three Private DNS zones with proper auto-registration. Option A is one Terraform `apply` against an existing subscription. For a pilot workload that may be revised, "hours" is the right cost.

## What this ADR does **not** override

ADR-002 (general network posture) **still applies** to:

- Any future loader that lands data with confidentiality value (PII, internal IP, customer data).
- Any workload whose MI has broader RBAC (e.g. Contributor on multiple workspaces).
- Any workload that the platform team's policy mandates be VNet-injected.

This ADR is narrowly scoped to the BOE loader. BOA, Endesa, REE, and any future loaders make their own decisions; defaulting to Option C for those is fine.

## Triggers to revisit (move to Option C)

We commit to re-evaluating this decision if any of the following becomes true:

1. **A policy is introduced** that denies public-network resources in this subscription, OR
2. **The Fabric workspace `lh_esp_origination`** starts holding non-public data alongside the BOE corpus, OR
3. **The MI's role scope expands** beyond AcrPull + one-workspace Contributor, OR
4. **A real incident** (leaked token, abuse) demonstrates the lack of network-layer defence was costly, OR
5. **The platform team finalises a baseline** that requires VNet-injection for all production workloads.

If any of these fires, the migration to Option C is a one-day Terraform change (add the `network` and `private-endpoints` modules; the loader code itself does not change).

## Reversibility

**High.** The migration path A → C:

1. Add the network module to Terraform (VNet, subnets, NSGs).
2. Recreate the ACA environment as workload-profiles + VNet-injected (this is destructive — the env must be replaced; the Jobs come back with it).
3. Add private endpoints + Private DNS zones.
4. Fabric tenant admin enables workspace inbound private link.
5. Approve the private endpoint from our spoke side.

Estimated effort: one day. The loader code does not change at all between A and C.

## What this means for the Terraform delivered today

The Terraform modules implementing this ADR ship in `infra/`:

- `infra/main.tf` — composition + provider config
- `infra/modules/identity/` — UAMI + role assignments
- `infra/modules/container-apps/` — ACA env (Consumption) + two Jobs
- `infra/modules/observability/` — diagnostic settings to existing Log Analytics

Notably **absent**:

- `infra/modules/network/` — not needed for Option A
- `infra/modules/private-endpoints/` — not needed for Option A
- Any reference to Private DNS zones

The composition is parameterised on `environment` (dev/stg/prod), `region` (`spaincentral`), and the existing ACR + Log Analytics resource IDs.

## Verification

After deploy:

1. The ACA Job has a **public IP** in the multi-tenant pool (`az containerapp job show ... --query "properties.outboundIpAddresses"`). This is expected for Option A.
2. The UAMI has exactly two role assignments: `AcrPull` on the specified ACR, and `Contributor` on the named Fabric workspace (the latter granted in Fabric, not Azure RBAC).
3. A manual trigger of `caj-boe-ingest-daily-dev` against a recent weekday writes PDFs into `Files/bronze/boe/raw/year=.../month=.../day=.../` in the dev lakehouse.
4. A trigger against a Sunday writes an empty-day manifest with no PDFs and no alert.
