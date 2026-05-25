#!/usr/bin/env python3
"""
Incremental sync of n8n official documentation to Hindsight.
Clones/pulls the n8n-docs repo and re-ingests changed markdown files.

State tracked in SYNC_STATE_FILE (default: /data/sync-docs-state.json).
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
BANK_ID = "n8n"
REPO_URL = "https://github.com/n8n-io/n8n-docs.git"
CLONE_DIR = "/tmp/n8n-docs-source"
STATE_FILE = os.environ.get("SYNC_DOCS_STATE_FILE", "/data/sync-docs-state.json")

SKIP_DIRS = {"_extra", "_images", "_includes", "_macros", "_video", "_workflows", "integrations"}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def clone_or_pull():
    if os.path.exists(os.path.join(CLONE_DIR, ".git")):
        subprocess.run(["git", "-C", CLONE_DIR, "pull", "--depth=1"], capture_output=True)
    else:
        subprocess.run(["git", "clone", "--depth=1", REPO_URL, CLONE_DIR], capture_output=True, timeout=300)
    sha = subprocess.run(["git", "-C", CLONE_DIR, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return sha


def get_changed_files(last_sha):
    """Get files changed since last sync. If no last_sha, return all."""
    if not last_sha:
        return None  # Signal to do full scan
    result = subprocess.run(
        ["git", "-C", CLONE_DIR, "diff", "--name-only", last_sha, "HEAD"],
        capture_output=True, text=True,
    )
    return [f for f in result.stdout.strip().split("\n") if f.endswith(".md") and f.startswith("docs/")]


def collect_all_docs():
    docs_dir = os.path.join(CLONE_DIR, "docs")
    files = []
    for root, dirs, filenames in os.walk(docs_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".md"):
                files.append(os.path.join(root, f))
    return files


def format_doc(filepath):
    rel = os.path.relpath(filepath, os.path.join(CLONE_DIR, "docs"))
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None

    # Strip frontmatter
    if content.startswith("---"):
        try:
            end = content.index("---", 3)
            content = content[end + 3:].strip()
        except ValueError:
            pass

    if len(content) < 50:
        return None

    content = content[:6000]
    section = rel.split("/")[0] if "/" in rel else "general"
    slug = rel.replace(".md", "").replace("/index", "")

    return {
        "content": content,
        "context": f"n8n docs - {slug}",
        "tags": ["type:docs", f"section:{section}"],
    }


def retain_batch(items):
    payload = json.dumps({"items": items, "async": True}).encode()
    headers = {"Authorization": f"Bearer {HINDSIGHT_KEY}", "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/memories",
        data=payload, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status in (200, 201, 202)
    except Exception as e:
        print(f"  RETAIN ERROR: {e}", file=sys.stderr, flush=True)
        return False


def main():
    if not HINDSIGHT_KEY:
        print("ERROR: HINDSIGHT_API_TENANT_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    full_run = "--full" in sys.argv

    sha = clone_or_pull()
    print(f"At commit: {sha[:12]}", flush=True)

    if full_run or not state.get("last_sha"):
        files = collect_all_docs()
        print(f"Full scan: {len(files)} docs", flush=True)
    else:
        changed = get_changed_files(state["last_sha"])
        if changed is None:
            files = collect_all_docs()
        else:
            files = [os.path.join(CLONE_DIR, f) for f in changed if os.path.exists(os.path.join(CLONE_DIR, f))]
        print(f"Incremental: {len(files)} changed docs", flush=True)

    if not files:
        print("Nothing new.", flush=True)
        state["last_sha"] = sha
        state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(state)
        return

    success = 0
    batch = []
    for filepath in files:
        item = format_doc(filepath)
        if not item:
            continue
        batch.append(item)
        if len(batch) >= 5:
            if retain_batch(batch):
                success += len(batch)
            batch = []

    if batch:
        if retain_batch(batch):
            success += len(batch)

    state["last_sha"] = sha
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["last_count"] = success
    save_state(state)
    print(f"Done: {success} docs retained", flush=True)


if __name__ == "__main__":
    main()
