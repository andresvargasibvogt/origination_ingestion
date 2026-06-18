#!/usr/bin/env bash
# Per-year historical backfill driver for BOE / BOA.
#
# Runs the dedicated backfill ACA Job one calendar year at a time, stepping from
# the first year toward the second (so newest-first is `... 2026 2019`). Each
# chunk uses the historical-aware filter (--relevance-profile=backfill) and is
# clamped so it never overlaps live daily data. The driver waits for each chunk
# to finish before starting the next (bounded load; lets the promoter drain).
#
# Usage:
#   scripts/backfill.sh boe 2026 2019      # BOE, newest year first
#   scripts/backfill.sh boa 2026 2019
#   scripts/backfill.sh boe 2026 2026      # just the 2026 gap
#
# Idempotent: re-running a year overwrites the same bronze partitions with
# identical bytes. Safe to re-run a failed year.
set -euo pipefail

RG="rg-origination"

SRC="${1:?usage: backfill.sh <boe|boa> <start_year> <end_year>}"
START_YEAR="${2:?missing start_year}"
END_YEAR="${3:?missing end_year}"

case "$SRC" in
  boe) JOB="caj-boe-backfill"; GOLIVE="2026-06-04" ;;   # first BOE daily-data date
  boa) JOB="caj-boa-backfill"; GOLIVE="2026-06-08" ;;   # first BOA daily-data date
  *) echo "source must be 'boe' or 'boa', got '$SRC'" >&2; exit 2 ;;
esac

# The newest chunk stops the day before daily go-live so we only fill the gap.
GOLIVE_MINUS_1="$(date -u -d "${GOLIVE} -1 day" +%F)"

# Direction: descending if START >= END, else ascending.
if (( START_YEAR >= END_YEAR )); then step=-1; else step=1; fi

echo "Backfill ${SRC^^} via ${JOB}: years ${START_YEAR}→${END_YEAR} (clamp to ≤ ${GOLIVE_MINUS_1})"

y="$START_YEAR"
while :; do
  FROM="${y}-01-01"
  TO="${y}-12-31"
  # Clamp the upper bound so we never touch live daily data.
  if [[ "$TO" > "$GOLIVE_MINUS_1" ]]; then TO="$GOLIVE_MINUS_1"; fi
  if [[ "$FROM" > "$GOLIVE_MINUS_1" ]]; then
    echo "[$y] skip — entirely inside the live daily range (> ${GOLIVE_MINUS_1})"
  else
    # Single-token form (FROM:TO) so it round-trips cleanly through az --args;
    # `az --args="a b c"` would collapse to ONE argv element with embedded
    # spaces, which argparse can't split. --backfill implies the backfill filter.
    ARG="--backfill=${FROM}:${TO}"
    echo ""
    echo "=== [$y] ${JOB} ${ARG} ==="
    az containerapp job update -n "$JOB" -g "$RG" --args="$ARG" -o none
    RUN="$(az containerapp job start -n "$JOB" -g "$RG" --query name -o tsv)"
    echo "    execution: $RUN — polling…"
    while :; do
      st="$(az containerapp job execution show -n "$JOB" -g "$RG" \
              --job-execution-name "$RUN" --query properties.status -o tsv 2>/dev/null || echo Unknown)"
      printf '    [%s] %s\n' "$(date -u +%H:%M:%S)" "$st"
      case "$st" in
        Succeeded) break ;;
        Failed|Stopped) echo "    chunk $y FAILED ($st) — fix + re-run 'scripts/backfill.sh $SRC $y $y'"; exit 1 ;;
      esac
      sleep 20
    done
  fi
  [[ "$y" == "$END_YEAR" ]] && break
  y=$(( y + step ))
done

echo ""
echo "Backfill ${SRC^^} ${START_YEAR}→${END_YEAR} complete. The promoter will drain"
echo "any remaining staged blobs into OneLake on its next ticks."
