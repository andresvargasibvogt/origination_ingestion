"""Read-only OneLake reconciliation for a backfilled source-year.

Counts the PDFs and manifests that actually landed in the lakehouse for a
given source + year, and sums the manifest item lists (which the promoter
dedupes by identifier). Use it to reconcile a backfill chunk:

    uv run python scripts/verify_onelake_year.py boa 2021

Distinct PDFs should equal the summed manifest item count. The per-day
"written" figure in the run logs can be slightly higher when two sumario
entries on the same day reference the same MLKOB — both write to the same
pdf_path (the second overwrites), so they count twice in logs but land as
one blob and one manifest entry. OneLake is ground truth.
"""

from __future__ import annotations

import json
import os
import sys

from azure.storage.filedatalake import DataLakeServiceClient

from origination_common.config import ONELAKE_ACCOUNT_URL
from origination_common.onelake import select_credential

WORKSPACE = os.getenv("FABRIC_WORKSPACE_NAME", "ws_esp_origination")
LAKEHOUSE = os.getenv("FABRIC_LAKEHOUSE_NAME", "lh_esp_origination")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: verify_onelake_year.py <source> <year>", file=sys.stderr)
        return 2
    source, year = sys.argv[1], sys.argv[2]

    svc = DataLakeServiceClient(
        account_url=ONELAKE_ACCOUNT_URL, credential=select_credential(CLIENT_ID)
    )
    fs = svc.get_file_system_client(WORKSPACE)
    prefix = f"{LAKEHOUSE}.Lakehouse/Files/bronze/{source}/raw"

    # 1. PDFs
    pdf_prefix = f"{prefix}/year={year}/"
    pdfs = [
        p.name for p in fs.get_paths(path=pdf_prefix, recursive=True)
        if p.name.endswith(".pdf")
    ]

    # 2. Manifests
    man_prefix = f"{prefix}/_manifests/year={year}/"
    man_paths = [
        p.name for p in fs.get_paths(path=man_prefix, recursive=True)
        if p.name.endswith("_manifest.json")
    ]

    total_items = 0
    nonempty = 0
    empty = 0
    item_ids: set[str] = set()
    for mp in man_paths:
        raw = fs.get_file_client(mp).download_file().readall()
        man = json.loads(raw)
        items = man.get("items", [])
        if items:
            nonempty += 1
            total_items += len(items)
            for it in items:
                item_ids.add(it.get("identifier", ""))
        else:
            empty += 1

    print(f"{source} {year}:")
    print(f"  PDFs in OneLake:        {len(pdfs)}")
    print(f"  manifests:              {len(man_paths)}  ({nonempty} with items, {empty} empty)")
    print(f"  summed manifest items:  {total_items}")
    print(f"  distinct identifiers:   {len(item_ids)}")
    match = "OK" if len(pdfs) == total_items else "MISMATCH"
    print(f"  PDFs == summed items?   {match}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
