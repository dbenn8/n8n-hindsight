#!/usr/bin/env python3
"""
Sync n8n codebase files to Hindsight via GitHub API.

Usage:
    python3 sync-code.py                  # incremental (changed since last sync)
    python3 sync-code.py --full           # re-ingest all files
    python3 sync-code.py --surgical       # ingest only files NOT already in bank
    python3 sync-code.py --dry-run        # show what would be synced
    python3 sync-code.py --test N         # sync only N files (for testing)

State tracked in SYNC_CODE_STATE_FILE (default: /data/sync-code-state.json).
"""
import base64
import json
import os
import sys
import time
import urllib.request

import sync_common

HINDSIGHT_URL, HINDSIGHT_KEY = sync_common.resolve_env()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
BANK_ID = sync_common.BANK_ID
REPO = "n8n-io/n8n"
STATE_FILE = os.environ.get("SYNC_CODE_STATE_FILE", "/data/sync-code-state.json")

INCLUDE_PACKAGES = ["cli", "core", "workflow", "@n8n"]
INCLUDE_EXTENSIONS = {".ts", ".js", ".vue"}
SKIP_PATTERNS = [
    "node_modules", "dist", "__tests__", ".test.", ".spec.",
    "test/", "tests/", ".d.ts", "coverage", ".stories.",
]

GITHUB_BASE = f"https://github.com/{REPO}/blob/master"


def load_state():
    return sync_common.load_state(STATE_FILE)


def save_state(state):
    sync_common.save_state(STATE_FILE, state)


def github_get(path):
    url = f"https://api.github.com/{path}"
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def should_include(filepath):
    if not any(filepath.startswith(f"packages/{pkg}/") for pkg in INCLUDE_PACKAGES):
        return False
    _, ext = os.path.splitext(filepath)
    if ext not in INCLUDE_EXTENSIONS:
        return False
    for skip in SKIP_PATTERNS:
        if skip in filepath:
            return False
    return True


def list_all_files():
    data = github_get(f"repos/{REPO}/git/trees/master?recursive=1")
    files = []
    for item in data.get("tree", []):
        if item["type"] == "blob" and should_include(item["path"]):
            files.append(item["path"])
    return sorted(files)


def get_changed_files(since_timestamp):
    commits = github_get(f"repos/{REPO}/commits?since={since_timestamp}&per_page=100")
    if not commits:
        return []
    changed = set()
    for commit in commits:
        detail = github_get(f"repos/{REPO}/commits/{commit['sha']}")
        for f in detail.get("files", []):
            if should_include(f["filename"]):
                changed.add(f["filename"])
        time.sleep(0.5)
    return sorted(changed)


def get_existing_filepaths():
    existing = set()
    offset = 0
    while True:
        url = f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/documents?tags=source:github-code&limit=100&offset={offset}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {HINDSIGHT_KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception:
            break
        items = data.get("items", [])
        for d in items:
            meta = d.get("document_metadata") or {}
            fp = meta.get("filepath")
            if fp:
                existing.add(fp)
        if len(items) < 100:
            break
        offset += 100
    return existing


def fetch_file_content(filepath):
    data = github_get(f"repos/{REPO}/contents/{filepath}")
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return data.get("content", "")


def format_file(filepath, content):
    if len(content) < 10:
        return None

    if len(content) > 8000:
        content = content[:8000] + "\n\n... [truncated]"

    parts = filepath.split("/")
    package = parts[1] if len(parts) > 1 else "root"
    url = f"{GITHUB_BASE}/{filepath}"
    slug = filepath.replace("/", "-").replace(".", "-")

    return {
        "document_id": f"code-{slug}",
        "content": f"n8n source code: {filepath}\n\n```\n{content}\n```",
        "context": f"n8n codebase - {filepath} ({url})",
        "tags": ["type:code", "source:github-code", f"package:{package}", "pipeline:doc_id"],
        "metadata": {"url": url, "filepath": filepath, "package": package},
    }


def retain_batch(items):
    return sync_common.retain_batch(items, HINDSIGHT_URL, HINDSIGHT_KEY, BANK_ID)


def main():
    parser = sync_common.build_arg_parser()
    parser.add_argument("--surgical", action="store_true")
    args = parser.parse_known_args()[0]
    full_run = args.full
    surgical = args.surgical
    dry_run = args.dry_run
    test_limit = args.test

    if not HINDSIGHT_KEY:
        print("ERROR: HINDSIGHT_API_TENANT_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    sync_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    last_sync = None if (full_run or surgical) else state.get("last_sync")

    if last_sync:
        print(f"Incremental: files changed since {last_sync}", flush=True)
        files = get_changed_files(last_sync)
    else:
        print("Full scan via tree API...", flush=True)
        files = list_all_files()

    print(f"Files on GitHub: {len(files)}", flush=True)

    if surgical:
        print("Querying existing docs in bank...", flush=True)
        existing = get_existing_filepaths()
        before = len(files)
        files = [f for f in files if f not in existing]
        print(f"Surgical: {before} total - {len(existing)} in bank = {len(files)} to ingest", flush=True)

    if test_limit:
        files = files[:test_limit]
        print(f"Test mode: {test_limit} files only", flush=True)

    if dry_run:
        for f in files[:20]:
            print(f"  {f}")
        if len(files) > 20:
            print(f"  ... and {len(files) - 20} more")
        print(f"\n=== DRY RUN: {len(files)} files ===", flush=True)
        return

    retained = 0
    skipped = 0
    failed = 0
    batch = []

    for i, filepath in enumerate(files):
        try:
            content = fetch_file_content(filepath)
        except Exception as e:
            print(f"  FETCH ERROR {filepath}: {e}", file=sys.stderr, flush=True)
            skipped += 1
            continue

        item = format_file(filepath, content)
        if not item:
            skipped += 1
            continue

        batch.append(item)

        if len(batch) >= 5:
            if retain_batch(batch):
                retained += len(batch)
            else:
                failed += len(batch)
            batch = []
            time.sleep(0.1)

        if (i + 1) % 50 == 0:
            print(f"  [{i + 1}/{len(files)}] retained={retained} skipped={skipped} failed={failed}", flush=True)

    if batch:
        if retain_batch(batch):
            retained += len(batch)
        else:
            failed += len(batch)

    state["last_sync"] = sync_start
    state["last_run"] = sync_start
    state["last_count"] = retained
    state["total_synced"] = state.get("total_synced", 0) + retained
    save_state(state)

    print(f"\n=== DONE: {retained} retained, {skipped} skipped, {failed} failed ===", flush=True)


if __name__ == "__main__":
    main()
