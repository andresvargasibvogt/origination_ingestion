#!/usr/bin/env bash
#
# Deploy script for the BOE ingest loader — per ADR-007 + ADR-008.
#
# Architecture: ingest writes PDFs to a staging Azure Storage Account
# (Defender for Storage enabled). A separate promoter ACA Job reads
# blob index tags on a morning schedule and promotes clean blobs to OneLake bronze.
#
# Idempotent: re-running is safe; each step checks if the resource exists
# before creating. Run from the repo root:
#
#   ./scripts/deploy-origination.sh
#
# Prerequisites:
#   - az CLI logged in (az login)
#   - Subscription access to 53cc82cf-f636-425d-8e25-f37b6bb8ef8f
#   - rg-origination already exists (created manually 2026-06-02)
#   - CONTRIBUTOR_GROUP_OBJECT_ID env var set (default: DEV Fabric SG)
#
# Admin actions required (script will warn and skip cleanly if blocked):
#   - AcrPull role on the ACR for the UAMI
#   - UAMI membership in the Fabric Central Members (DEV) security group
#   - Storage Blob Data Contributor on the staging account for the UAMI
#   - Defender for Storage enabled on the staging account (subscription
#     or per-account)

set -euo pipefail

# ─── Configuration (locked per ADR-007 + ADR-008) ────────────────────────
SUB="53cc82cf-f636-425d-8e25-f37b6bb8ef8f"
RG="rg-origination"
LOC="northeurope"

ACR_NAME="${ACR_NAME:-acrorigination}"          # globally unique; override if taken
LAW="log-origination"
UAMI="id-origination"
ACA_ENV="cae-origination"
JOB_DAILY="caj-boe-daily"
JOB_BACKFILL="caj-boe-backfill"
JOB_PROMOTER="caj-promoter"
JOB_ENDESA="caj-endesa-monthly"   # e-distribución monthly CSV poller (mirrors caj-ree-monthly)
# NOTE: caj-boa-daily and caj-ree-monthly were created out-of-band (CLI) and are
# not yet codified here — this script is BOE + promoter + endesa only for now.

# Staging Blob Storage Account (ADR-008) — globally unique
STG_ACCT="${STG_ACCT:-storiginationdmz}"        # 3-24 chars, lowercase alphanumeric
STG_CONTAINER_UNTRUSTED="untrusted"
STG_CONTAINER_QUARANTINE="quarantine"

WORKSPACE_NAME="Central Data & Integration (DEV)"
LAKEHOUSE_NAME="lh_esp_origination"

# Entra security group that has Fabric Contributor on the target workspace.
# Default is the DEV group "Fabric - Central Members (DEV)" (resolved 2026-06-03).
CONTRIBUTOR_GROUP_OBJECT_ID="${CONTRIBUTOR_GROUP_OBJECT_ID:-a478fcaa-0a99-4c05-aaa1-640f8d2ef5dc}"

# Image config — single unified image, one entry point per source + promoter.
# NOTE: the repo is `origination-ingest` (matches the Dockerfile name) — this is
# the image ALL deployed Jobs reference. (`boe-ingest` is a legacy repo from the
# BOE-only era; nothing uses it. Building the wrong name silently no-ops deploys.)
IMAGE_NAME="origination-ingest"
IMAGE_TAG="${IMAGE_TAG:-latest}"
DOCKERFILE="containers/origination-ingest.Dockerfile"

# ─── Helpers ─────────────────────────────────────────────────────────────
log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[%s] WARN:\033[0m %s\n' "$(date -u +%H:%M:%S)" "$*"; }
err()  { printf '\033[1;31m[%s] ERROR:\033[0m %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "$1 not found in PATH"; exit 1; }
}

az_resource_exists() {
  # Returns 0 if resource exists, 1 if not. Suppresses errors.
  az "$@" >/dev/null 2>&1
}

# ─── Pre-flight ──────────────────────────────────────────────────────────
require_cmd az
require_cmd docker || true   # only needed if building locally; az acr build runs server-side

log "Setting subscription to $SUB"
az account set --subscription "$SUB"

log "Verifying $RG exists in $LOC"
if ! az group show -n "$RG" >/dev/null 2>&1; then
  err "$RG does not exist. Create it with: az group create -n $RG -l $LOC"
  exit 1
fi
ACTUAL_LOC=$(az group show -n "$RG" --query location -o tsv)
if [[ "$ACTUAL_LOC" != "$LOC" ]]; then
  err "$RG is in $ACTUAL_LOC, but ADR-007 specifies $LOC. Cannot proceed."
  exit 1
fi
log "✓ $RG exists in $LOC"

# ─── 1. Azure Container Registry (new, dedicated) ────────────────────────
log "Step 1: ACR $ACR_NAME"
if az_resource_exists acr show -n "$ACR_NAME" -g "$RG"; then
  log "✓ $ACR_NAME already exists in $RG"
else
  az acr create -n "$ACR_NAME" -g "$RG" -l "$LOC" --sku Basic --admin-enabled false >/dev/null
  log "✓ Created $ACR_NAME"
fi
ACR_ID=$(az acr show -n "$ACR_NAME" -g "$RG" --query id -o tsv)

# ─── 2. Log Analytics workspace (new, dedicated) ─────────────────────────
log "Step 2: Log Analytics $LAW"
if az_resource_exists monitor log-analytics workspace show -n "$LAW" -g "$RG"; then
  log "✓ $LAW already exists in $RG"
else
  az monitor log-analytics workspace create -n "$LAW" -g "$RG" -l "$LOC" --sku PerGB2018 >/dev/null
  log "✓ Created $LAW"
fi
LAW_CUSTOMER_ID=$(az monitor log-analytics workspace show -n "$LAW" -g "$RG" --query customerId -o tsv)
LAW_SHARED_KEY=$(az monitor log-analytics workspace get-shared-keys -n "$LAW" -g "$RG" --query primarySharedKey -o tsv)

# ─── 3. User-assigned managed identity ───────────────────────────────────
log "Step 3: Managed identity $UAMI"
if az_resource_exists identity show -n "$UAMI" -g "$RG"; then
  log "✓ $UAMI already exists"
else
  az identity create -n "$UAMI" -g "$RG" -l "$LOC" >/dev/null
  log "✓ Created $UAMI"
fi
UAMI_ID=$(az identity show -n "$UAMI" -g "$RG" --query id -o tsv)
UAMI_CLIENT_ID=$(az identity show -n "$UAMI" -g "$RG" --query clientId -o tsv)
UAMI_PRINCIPAL_ID=$(az identity show -n "$UAMI" -g "$RG" --query principalId -o tsv)

# ─── 4. Grant AcrPull on the new ACR to the UAMI ─────────────────────────
# Use --include-inherited so RG-scope (or higher) AcrPull grants are detected
# and we don't try to create a duplicate at the ACR scope.
log "Step 4: Role assignment — AcrPull on $ACR_NAME for $UAMI"
if az role assignment list --assignee "$UAMI_PRINCIPAL_ID" --scope "$ACR_ID" --include-inherited --query "[?roleDefinitionName=='AcrPull']" -o tsv 2>/dev/null | grep -q .; then
  log "✓ AcrPull already effective (direct or inherited)"
else
  if az role assignment create \
       --assignee-object-id "$UAMI_PRINCIPAL_ID" \
       --assignee-principal-type ServicePrincipal \
       --role AcrPull \
       --scope "$ACR_ID" >/dev/null 2>&1; then
    log "✓ AcrPull granted"
  else
    warn "Could not create AcrPull role assignment — likely missing User Access Administrator permission."
    warn "Ask the platform team to grant AcrPull to:"
    warn "  Principal:  $UAMI_PRINCIPAL_ID"
    warn "  Scope:      $ACR_ID (or any parent scope works via inheritance)"
    warn "Continuing — Jobs will fail to pull the image until this is granted."
  fi
fi

# ─── 5. Add UAMI to the existing Entra security group ────────────────────
log "Step 5: Entra group membership"
if [[ -z "$CONTRIBUTOR_GROUP_OBJECT_ID" ]]; then
  warn "CONTRIBUTOR_GROUP_OBJECT_ID is not set — SKIPPING step 5."
elif az ad group member check --group "$CONTRIBUTOR_GROUP_OBJECT_ID" --member-id "$UAMI_PRINCIPAL_ID" --query value -o tsv 2>/dev/null | grep -q true; then
  log "✓ UAMI already a member of the security group"
else
  if az ad group member add --group "$CONTRIBUTOR_GROUP_OBJECT_ID" --member-id "$UAMI_PRINCIPAL_ID" 2>/dev/null; then
    log "✓ UAMI added to the security group"
  else
    warn "Could not add UAMI to group $CONTRIBUTOR_GROUP_OBJECT_ID — likely missing group-owner / directory permission."
    warn "Ask the platform team to add the managed identity (principal ID below) to:"
    warn "  Group:    Fabric - Central Members (DEV) (object ID: $CONTRIBUTOR_GROUP_OBJECT_ID)"
    warn "  Member:   $UAMI_PRINCIPAL_ID"
    warn "Continuing — Jobs will fail at OneLake write until this is granted."
  fi
fi

# ─── 6. Staging Storage Account (ADR-008) ────────────────────────────────
log "Step 6: Staging Storage Account $STG_ACCT"
if az_resource_exists storage account show -n "$STG_ACCT" -g "$RG"; then
  log "✓ $STG_ACCT already exists in $RG"
else
  # Storage account names must be globally unique. If $STG_ACCT is taken,
  # the create will fail with NameAlreadyTaken — override via $STG_ACCT env var.
  if ! az storage account create \
        -n "$STG_ACCT" -g "$RG" -l "$LOC" \
        --sku Standard_LRS --kind StorageV2 \
        --min-tls-version TLS1_2 \
        --allow-blob-public-access false >/dev/null 2>&1; then
    err "Failed to create storage account $STG_ACCT. If the name is taken globally,"
    err "  override via: STG_ACCT=storiginationdmz01 ./scripts/deploy-origination.sh"
    exit 1
  fi
  log "✓ Created $STG_ACCT"
fi
STG_ACCT_ID=$(az storage account show -n "$STG_ACCT" -g "$RG" --query id -o tsv)

# Create both containers (idempotent — create-if-missing semantics)
for c in "$STG_CONTAINER_UNTRUSTED" "$STG_CONTAINER_QUARANTINE"; do
  if az storage container exists --account-name "$STG_ACCT" --name "$c" --auth-mode login --query exists -o tsv 2>/dev/null | grep -q true; then
    log "✓ Container $c exists"
  else
    if az storage container create --account-name "$STG_ACCT" --name "$c" --auth-mode login >/dev/null 2>&1; then
      log "✓ Created container $c"
    else
      warn "Could not create container $c via --auth-mode login; falling back to key auth..."
      az storage container create --account-name "$STG_ACCT" --name "$c" >/dev/null
      log "✓ Created container $c (via key auth)"
    fi
  fi
done

# Grant UAMI Storage Blob Data Contributor on the staging account
log "Step 6a: Role assignment — Storage Blob Data Contributor on $STG_ACCT for $UAMI"
if az role assignment list --assignee "$UAMI_PRINCIPAL_ID" --scope "$STG_ACCT_ID" --include-inherited --query "[?roleDefinitionName=='Storage Blob Data Contributor']" -o tsv 2>/dev/null | grep -q .; then
  log "✓ Role already effective (direct or inherited)"
else
  if az role assignment create \
       --assignee-object-id "$UAMI_PRINCIPAL_ID" \
       --assignee-principal-type ServicePrincipal \
       --role "Storage Blob Data Contributor" \
       --scope "$STG_ACCT_ID" >/dev/null 2>&1; then
    log "✓ Role granted"
  else
    warn "Could not create role assignment — likely missing User Access Administrator."
    warn "Ask the platform team to grant 'Storage Blob Data Contributor' to:"
    warn "  Principal: $UAMI_PRINCIPAL_ID"
    warn "  Scope:     $STG_ACCT_ID"
    warn "Continuing — ingest Job will fail at staging write until granted."
  fi
fi

# Enable Defender for Storage on the account (per-account scope)
log "Step 6b: Defender for Storage on $STG_ACCT"
DEFENDER_STATE=$(az security atp storage show --storage-account "$STG_ACCT" -g "$RG" --query isEnabled -o tsv 2>/dev/null || echo "unknown")
if [[ "$DEFENDER_STATE" == "true" ]]; then
  log "✓ Defender for Storage already enabled"
else
  if az security atp storage update --storage-account "$STG_ACCT" -g "$RG" --is-enabled true >/dev/null 2>&1; then
    log "✓ Defender for Storage enabled"
  else
    warn "Could not enable Defender for Storage — needs subscription Owner / Security Admin permission."
    warn "Ask the platform team to enable Defender for Storage on:"
    warn "  Storage account: $STG_ACCT (resource ID: $STG_ACCT_ID)"
    warn "  Plan:            Defender for Storage v2 with malware scanning"
    warn "  Reference:       https://learn.microsoft.com/azure/defender-for-cloud/tutorial-enable-storage-plan"
    warn "Continuing — uploaded blobs will NOT be scanned until this is enabled."
  fi
fi

# ─── 7. Build + push the image to the new ACR ────────────────────────────
log "Step 7: Build + push image $IMAGE_NAME:$IMAGE_TAG"
az acr build -r "$ACR_NAME" -t "${IMAGE_NAME}:${IMAGE_TAG}" -f "$DOCKERFILE" . >/dev/null
log "✓ Image built and pushed to $ACR_NAME.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG}"
IMAGE_REF="$ACR_NAME.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG}"

# ─── 8. ACA Environment ──────────────────────────────────────────────────
log "Step 8: ACA Environment $ACA_ENV"
if az_resource_exists containerapp env show -n "$ACA_ENV" -g "$RG"; then
  log "✓ $ACA_ENV already exists"
else
  az containerapp env create \
    -n "$ACA_ENV" -g "$RG" -l "$LOC" \
    --logs-destination log-analytics \
    --logs-workspace-id "$LAW_CUSTOMER_ID" \
    --logs-workspace-key "$LAW_SHARED_KEY" >/dev/null
  log "✓ Created $ACA_ENV"
fi

# Common env vars used by all Jobs.
COMMON_ENV=(
  FABRIC_WORKSPACE_NAME="$WORKSPACE_NAME"
  FABRIC_LAKEHOUSE_NAME="$LAKEHOUSE_NAME"
  AZURE_CLIENT_ID="$UAMI_CLIENT_ID"
  # User-Agent is no longer overridden here — the per-source config default
  # ("Mozilla/5.0 (compatible; iBVogt-DataPlatform)", no PII) is the single
  # source of truth. Set {BOE,BOA,REE}_USER_AGENT only to deviate.
  STG_ACCOUNT_NAME="$STG_ACCT"
  STG_CONTAINER_UNTRUSTED="$STG_CONTAINER_UNTRUSTED"
  STG_CONTAINER_QUARANTINE="$STG_CONTAINER_QUARANTINE"
)

# ─── 9. ACA Job — BOE daily (ingest writes to staging) ───────────────────
log "Step 9: ACA Job $JOB_DAILY"
if az_resource_exists containerapp job show -n "$JOB_DAILY" -g "$RG"; then
  log "✓ $JOB_DAILY already exists — updating image"
  az containerapp job update \
    -n "$JOB_DAILY" -g "$RG" \
    --image "$IMAGE_REF" \
    --replace-env-vars "${COMMON_ENV[@]}" \
    --args "--date today" >/dev/null
else
  az containerapp job create \
    -n "$JOB_DAILY" -g "$RG" \
    --environment "$ACA_ENV" \
    --trigger-type Schedule \
    --cron-expression "0 7 * * 1-6" \
    --replica-timeout 1800 \
    --replica-retry-limit 1 \
    --parallelism 1 \
    --replica-completion-count 1 \
    --image "$IMAGE_REF" \
    --cpu 0.5 --memory 1Gi \
    --mi-user-assigned "$UAMI_ID" \
    --registry-server "$ACR_NAME.azurecr.io" \
    --registry-identity "$UAMI_ID" \
    --env-vars "${COMMON_ENV[@]}" \
    --args "--date today" >/dev/null
  log "✓ Created $JOB_DAILY"
fi

# ─── 10. ACA Job — BOE backfill (ingest writes to staging) ───────────────
log "Step 10: ACA Job $JOB_BACKFILL"
if az_resource_exists containerapp job show -n "$JOB_BACKFILL" -g "$RG"; then
  log "✓ $JOB_BACKFILL already exists — updating image"
  az containerapp job update \
    -n "$JOB_BACKFILL" -g "$RG" \
    --image "$IMAGE_REF" \
    --replace-env-vars "${COMMON_ENV[@]}" >/dev/null
else
  az containerapp job create \
    -n "$JOB_BACKFILL" -g "$RG" \
    --environment "$ACA_ENV" \
    --trigger-type Manual \
    --replica-timeout 3600 \
    --replica-retry-limit 1 \
    --image "$IMAGE_REF" \
    --cpu 0.5 --memory 1Gi \
    --mi-user-assigned "$UAMI_ID" \
    --registry-server "$ACR_NAME.azurecr.io" \
    --registry-identity "$UAMI_ID" \
    --env-vars "${COMMON_ENV[@]}" >/dev/null
  log "✓ Created $JOB_BACKFILL"
fi

# ─── 11. ACA Job — promoter (twice-hourly, mornings UTC; ADR-008) ────────
log "Step 11: ACA Job $JOB_PROMOTER"
if az_resource_exists containerapp job show -n "$JOB_PROMOTER" -g "$RG"; then
  log "✓ $JOB_PROMOTER already exists — updating image"
  az containerapp job update \
    -n "$JOB_PROMOTER" -g "$RG" \
    --image "$IMAGE_REF" \
    --replace-env-vars "${COMMON_ENV[@]}" \
    --command "promoter" >/dev/null
else
  az containerapp job create \
    -n "$JOB_PROMOTER" -g "$RG" \
    --environment "$ACA_ENV" \
    --trigger-type Schedule \
    --cron-expression "15,45 7,8,9 * * 1-6" \
    --replica-timeout 600 \
    --replica-retry-limit 1 \
    --parallelism 1 \
    --replica-completion-count 1 \
    --image "$IMAGE_REF" \
    --cpu 0.25 --memory 0.5Gi \
    --mi-user-assigned "$UAMI_ID" \
    --registry-server "$ACR_NAME.azurecr.io" \
    --registry-identity "$UAMI_ID" \
    --env-vars "${COMMON_ENV[@]}" \
    --command "promoter" >/dev/null
  log "✓ Created $JOB_PROMOTER"
fi

# ─── 11b. ACA Job — e-distribución monthly poller (mirrors caj-ree-monthly) ──
# Monthly CSV on an uncertain release day → poll daily, dedup against OneLake,
# land once when a new month appears. Month-level partitions only.
log "Step 11b: ACA Job $JOB_ENDESA"
if az_resource_exists containerapp job show -n "$JOB_ENDESA" -g "$RG"; then
  log "✓ $JOB_ENDESA already exists — updating image"
  az containerapp job update \
    -n "$JOB_ENDESA" -g "$RG" \
    --image "$IMAGE_REF" \
    --replace-env-vars "${COMMON_ENV[@]}" \
    --command "endesa-ingest" >/dev/null
else
  az containerapp job create \
    -n "$JOB_ENDESA" -g "$RG" \
    --environment "$ACA_ENV" \
    --trigger-type Schedule \
    --cron-expression "0 9 * * *" \
    --replica-timeout 1800 \
    --replica-retry-limit 1 \
    --parallelism 1 \
    --replica-completion-count 1 \
    --image "$IMAGE_REF" \
    --cpu 0.5 --memory 1Gi \
    --mi-user-assigned "$UAMI_ID" \
    --registry-server "$ACR_NAME.azurecr.io" \
    --registry-identity "$UAMI_ID" \
    --env-vars "${COMMON_ENV[@]}" \
    --command "endesa-ingest" >/dev/null
  log "✓ Created $JOB_ENDESA"
fi

# ─── 12. Summary + next steps ────────────────────────────────────────────
echo
log "Deploy complete."
echo "  Subscription:      $SUB"
echo "  Resource group:    $RG ($LOC)"
echo "  ACR:               $ACR_NAME"
echo "  Log Analytics:     $LAW"
echo "  Managed identity:  $UAMI"
echo "  UAMI principal ID: $UAMI_PRINCIPAL_ID"
echo "  Staging account:   $STG_ACCT (containers: $STG_CONTAINER_UNTRUSTED, $STG_CONTAINER_QUARANTINE)"
echo "  ACA Environment:   $ACA_ENV"
echo "  ACA Jobs:          $JOB_DAILY, $JOB_BACKFILL, $JOB_PROMOTER"
echo "  Image:             $IMAGE_REF"
echo "  Fabric target:     $WORKSPACE_NAME → $LAKEHOUSE_NAME"
echo
echo "To trigger a manual ingest run:"
echo "    az containerapp job start -n $JOB_DAILY -g $RG"
echo "To trigger a manual promoter run:"
echo "    az containerapp job start -n $JOB_PROMOTER -g $RG"
echo "Then watch executions:"
echo "    az containerapp job execution list -n $JOB_DAILY    -g $RG -o table"
echo "    az containerapp job execution list -n $JOB_PROMOTER -g $RG -o table"
