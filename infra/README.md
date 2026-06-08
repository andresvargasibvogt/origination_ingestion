# Infrastructure (Terraform, Option A per ADR-006)

Terraform for the BOE ingest loader, deployed to Azure Container Apps Jobs
in Spain Central.

## Layout

```text
infra/
  modules/
    identity/         user-assigned MI + role assignments
    container-apps/   ACA env (Consumption, multi-tenant) + two ACA Jobs
    observability/    diagnostic settings → existing Log Analytics
  environments/
    dev/              DEV composition (calls modules with DEV inputs)
    stg/              STG composition
    prod/             PROD composition
```

Each environment is a separate Terraform root module with its own state.
The actual resource definitions live in `modules/` and are shared.

## Backend state

Each environment expects a remote state backend already configured by your
platform team. The `backend "azurerm"` block is intentionally **not**
committed here — fill it in via `backend.hcl` per environment when running
`terraform init -backend-config=backend.hcl`, or wire it in your CI's
init step. This mirrors the convention you said the team already uses.

## What's deliberately absent (Option A choice — ADR-006)

- No `network` module (no spoke VNet, no subnets, no NSGs)
- No `private-endpoints` module (no ACR PE, no OneLake PE)
- No Private DNS zones

If the platform decides to upgrade to Option C, the migration path is
documented in [ADR-006](../docs/decisions/0006-bo-ingest-network-posture-option-a.md).

## Inputs each environment needs

- Existing ACR resource ID (registry that holds the boe-ingest image)
- Existing Log Analytics workspace resource ID (for diagnostic settings)
- Fabric workspace **name** (e.g. `Central Data & Integration (DEV)`) —
  passed to the Job as `FABRIC_WORKSPACE_NAME` env var
- Image tag to deploy (set per release, defaults to `latest`)

## What you still have to do manually after deploy

These cross the Azure ↔ Fabric boundary, Terraform can't do them:

1. **Grant the UAMI `Contributor` on the Fabric workspace.** Fabric
   workspace settings → Manage access → Add the UAMI by object ID (the
   Terraform output prints it).
2. **Approve the ACR role assignment** — actually Terraform does this
   one (AcrPull on the existing ACR), but verify with
   `az role assignment list --assignee <uami-objectId>`.

## Deploy

From `infra/environments/dev/`:

```bash
terraform init -backend-config=backend.hcl
terraform plan -var-file=dev.tfvars
terraform apply -var-file=dev.tfvars
```

Repeat in `stg/` and `prod/` when promoting.
