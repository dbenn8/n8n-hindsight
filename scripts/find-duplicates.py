#!/usr/bin/env python3
"""
Scan n8n Hindsight bank for duplicate documents.
Matching criteria (strict, per-type):
  - github-issue: same metadata.number + tag type:github-issue (NOT type:github-pr)
  - github-pr: same metadata.number + tag type:github-pr (NOT type:github-issue)
  - docs: same metadata.filepath
  - community-post: same metadata.topic_id
  - code: same metadata.filepath
  - release-notes: same metadata.version
Keeps the NEWEST document (most recent updated_at) per group.
"""
import json
import os
import sys
import urllib.request

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
BANK_ID = "n8n"
STATE_DIR = os.environ.get("DEDUP_STATE_DIR", ".")


def fetch_documents(offset=0, limit=100, tags=None):
    url = f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/documents?limit={limit}&offset={offset}"
    if tags:
        url += f"&tags={tags}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {HINDSIGHT_KEY}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_doc_key(doc):
    """Return (type, unique_key) or None if no dedup key found."""
    tags = set(doc.get("tags", []))
    meta = doc.get("document_metadata") or {}
    ctx = (doc.get("retain_params") or {}).get("context", "")

    if "type:github-issue" in tags and "type:github-pr" not in tags:
        num = meta.get("number")
        if num:
            return ("github-issue", num)

    elif "type:github-pr" in tags and "type:github-issue" not in tags:
        num = meta.get("number")
        if num:
            return ("github-pr", num)

    elif "type:docs" in tags:
        fp = meta.get("filepath")
        if fp:
            return ("docs", fp)

    elif "type:community-post" in tags:
        tid = meta.get("topic_id")
        if tid:
            return ("community-post", tid)

    elif "type:code" in tags:
        fp = meta.get("filepath")
        if fp:
            return ("code", fp)

    elif "type:release-notes" in tags:
        ver = meta.get("version")
        if ver:
            return ("release-notes", ver)

    return None


def main():
    dry_run = "--dry-run" in sys.argv or len(sys.argv) == 1

    # Fetch ALL documents
    all_docs = []
    offset = 0
    batch_size = 100
    total = None

    print("Fetching all documents...", flush=True)
    while True:
        data = fetch_documents(offset=offset, limit=batch_size)
        if total is None:
            total = data.get("total", 0)
            print(f"  Total documents: {total}", flush=True)

        items = data.get("items", [])
        if not items:
            break
        all_docs.extend(items)
        offset += len(items)
        if offset % 1000 == 0:
            print(f"  Fetched {offset}/{total}...", flush=True)
        if offset >= total:
            break

    print(f"\nFetched {len(all_docs)} documents total", flush=True)

    # Save all docs for delete-duplicates.py
    all_docs_path = os.path.join(STATE_DIR, "n8n-all-docs.json")
    with open(all_docs_path, "w") as f:
        json.dump(all_docs, f, indent=2)
    print(f"Saved all docs to {all_docs_path}")

    # Group by dedup key
    groups = {}  # (type, key) -> [docs]
    no_key = 0
    for doc in all_docs:
        key = get_doc_key(doc)
        if key is None:
            no_key += 1
            continue
        groups.setdefault(key, []).append(doc)

    # Find groups with duplicates
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}

    # Stats
    print(f"\n=== DUPLICATE ANALYSIS ===")
    print(f"Total documents: {len(all_docs)}")
    print(f"Documents with dedup key: {len(all_docs) - no_key}")
    print(f"Documents without dedup key: {no_key}")
    print(f"Unique groups: {len(groups)}")
    print(f"Groups with duplicates: {len(dup_groups)}")

    # Break down by type
    type_stats = {}
    to_delete = []
    for (dtype, key), docs in dup_groups.items():
        if dtype not in type_stats:
            type_stats[dtype] = {"groups": 0, "total_docs": 0, "to_delete": 0}
        type_stats[dtype]["groups"] += 1
        type_stats[dtype]["total_docs"] += len(docs)

        # Keep the doc with richest metadata + most extracted knowledge
        def richness(d):
            meta = d.get("document_metadata") or {}
            meta_fields = len([v for v in meta.values() if v])
            mem_units = d.get("memory_unit_count", 0)
            text_len = d.get("text_length", 0)
            tags_count = len(d.get("tags", []))
            updated = d.get("updated_at", "")
            return (meta_fields, mem_units, text_len, tags_count, updated)

        docs.sort(key=richness, reverse=True)
        keep = docs[0]
        delete = docs[1:]
        type_stats[dtype]["to_delete"] += len(delete)
        for d in delete:
            to_delete.append({
                "id": d["id"],
                "type": dtype,
                "key": key,
                "updated_at": d.get("updated_at", ""),
                "keep_id": keep["id"],
            })

    print(f"\nBy type:")
    for dtype, stats in sorted(type_stats.items()):
        print(f"  {dtype}: {stats['groups']} dup groups, {stats['total_docs']} docs, {stats['to_delete']} to delete")

    print(f"\nTotal documents to delete: {len(to_delete)}")
    print(f"Documents to keep: {len(all_docs) - len(to_delete)}")

    # Show some examples
    print(f"\n=== SAMPLE DUPLICATES ===")
    shown = {}
    for item in to_delete[:50]:
        dtype = item["type"]
        if shown.get(dtype, 0) >= 3:
            continue
        shown[dtype] = shown.get(dtype, 0) + 1
        print(f"  DELETE {dtype} key={item['key']} doc={item['id'][:12]} updated={item['updated_at'][:19]}")
        print(f"    KEEP doc={item['keep_id'][:12]}")

    if dry_run:
        print(f"\n=== DRY RUN — no deletions performed ===")
        # Save to JSON for later use
        with open(os.path.join(STATE_DIR, "n8n-duplicates.json"), "w") as f:
            json.dump(to_delete, f, indent=2)
        print(f"Saved {len(to_delete)} deletion targets to /tmp/n8n-duplicates.json")
    else:
        print(f"\n=== DELETING {len(to_delete)} duplicate documents ===")
        deleted = 0
        failed = 0
        for item in to_delete:
            try:
                req = urllib.request.Request(
                    f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/documents/{item['id']}",
                    headers={"Authorization": f"Bearer {HINDSIGHT_KEY}"},
                    method="DELETE",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    deleted += 1
            except Exception as e:
                failed += 1
                if failed <= 3:
                    print(f"  DELETE FAILED {item['id'][:12]}: {e}", flush=True)
            if (deleted + failed) % 100 == 0:
                print(f"  [{deleted + failed}/{len(to_delete)}] deleted={deleted} failed={failed}", flush=True)

        print(f"\n=== DONE: {deleted} deleted, {failed} failed ===")


if __name__ == "__main__":
    main()
