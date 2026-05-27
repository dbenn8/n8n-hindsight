#!/usr/bin/env python3
"""
Delete duplicate documents from n8n Hindsight bank, then reprocess keepers
where the deleted duplicate had more memory units.

Usage:
    python3 delete-duplicates.py --dry-run     # show what would happen
    python3 delete-duplicates.py               # execute deletes + reprocesses

Requires: n8n-duplicates.json and n8n-all-docs.json from find-duplicates.py (in DEDUP_STATE_DIR)
"""
import json
import os
import sys
import time
import urllib.request

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
BANK_ID = "n8n"
STATE_DIR = os.environ.get("DEDUP_STATE_DIR", ".")


def api_call(method, path, data=None):
    url = f"{HINDSIGHT_URL}{path}"
    headers = {"Authorization": f"Bearer {HINDSIGHT_KEY}"}
    if data:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    dry_run = "--dry-run" in sys.argv

    # Load data
    with open(os.path.join(STATE_DIR, "n8n-all-docs.json")) as f:
        all_docs = json.load(f)
    doc_by_id = {d["id"]: d for d in all_docs}

    with open(os.path.join(STATE_DIR, "n8n-duplicates.json")) as f:
        dupes = json.load(f)

    # Build groups
    groups = {}
    for d in dupes:
        gkey = (d["type"], d["key"])
        if gkey not in groups:
            groups[gkey] = {"keep_id": d["keep_id"], "discard_ids": [], "type": d["type"], "key": d["key"]}
        groups[gkey]["discard_ids"].append(d["id"])

    # Categorize
    to_delete = []  # (doc_id, type, key)
    to_reprocess = []  # keeper doc_ids where discard had more units

    for gkey, g in groups.items():
        keep = doc_by_id.get(g["keep_id"], {})
        k_units = keep.get("memory_unit_count", 0)

        for did in g["discard_ids"]:
            discard = doc_by_id.get(did, {})
            d_units = discard.get("memory_unit_count", 0)
            to_delete.append(did)

            if d_units > k_units:
                # Only add keeper to reprocess list once
                if g["keep_id"] not in [r for r in to_reprocess]:
                    to_reprocess.append(g["keep_id"])

    print(f"Documents to delete:    {len(to_delete)}")
    print(f"Keepers to reprocess:   {len(to_reprocess)}")
    print()

    if dry_run:
        print("=== DRY RUN — no changes made ===")
        print(f"\nSample deletes:")
        for did in to_delete[:10]:
            doc = doc_by_id.get(did, {})
            ctx = (doc.get("retain_params") or {}).get("context", "")[:60]
            print(f"  DELETE {did[:16]}  units={doc.get('memory_unit_count',0)}  {ctx}")
        print(f"\nSample reprocesses:")
        for kid in to_reprocess[:10]:
            doc = doc_by_id.get(kid, {})
            ctx = (doc.get("retain_params") or {}).get("context", "")[:60]
            print(f"  REPROCESS {kid[:16]}  units={doc.get('memory_unit_count',0)}  {ctx}")
        return

    # Phase 1: Delete duplicates
    print("=== PHASE 1: Deleting duplicate documents ===")
    deleted = 0
    failed = 0
    for did in to_delete:
        try:
            result = api_call("DELETE", f"/v1/default/banks/{BANK_ID}/documents/{did}")
            deleted += 1
        except Exception as e:
            failed += 1
            if failed <= 5:
                print(f"  DELETE FAILED {did[:16]}: {e}", flush=True)
        if (deleted + failed) % 100 == 0:
            print(f"  [{deleted + failed}/{len(to_delete)}] deleted={deleted} failed={failed}", flush=True)
        time.sleep(0.05)

    print(f"\nPhase 1 done: {deleted} deleted, {failed} failed")

    # Phase 2: Reprocess keepers where discard had more units
    print(f"\n=== PHASE 2: Reprocessing {len(to_reprocess)} keepers ===")
    reprocessed = 0
    rp_failed = 0
    for kid in to_reprocess:
        try:
            result = api_call("POST", f"/v1/default/banks/{BANK_ID}/documents/{kid}/reprocess")
            reprocessed += 1
        except Exception as e:
            rp_failed += 1
            if rp_failed <= 5:
                print(f"  REPROCESS FAILED {kid[:16]}: {e}", flush=True)
        if (reprocessed + rp_failed) % 50 == 0:
            print(f"  [{reprocessed + rp_failed}/{len(to_reprocess)}] reprocessed={reprocessed} failed={rp_failed}", flush=True)
        time.sleep(0.1)

    print(f"\nPhase 2 done: {reprocessed} reprocessed, {rp_failed} failed")
    print(f"\n=== COMPLETE ===")
    print(f"Deleted:      {deleted}/{len(to_delete)}")
    print(f"Reprocessed:  {reprocessed}/{len(to_reprocess)}")


if __name__ == "__main__":
    main()
