#!/usr/bin/env python3
"""
Sync n8n official documentation to Hindsight via GitHub API.
No local clone needed — fetches file list and content directly from GitHub.

Usage:
    python3 sync-docs.py                  # incremental (only changed files)
    python3 sync-docs.py --full           # re-ingest all docs
    python3 sync-docs.py --dry-run        # show what would be synced
    python3 sync-docs.py --test N         # sync only N files (for testing)

State tracked in SYNC_STATE_FILE (default: /data/sync-docs-state.json).
"""
import base64
import json
import os
import sys
import time
import urllib.request

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
BANK_ID = "n8n"
REPO = "n8n-io/n8n-docs"
STATE_FILE = os.environ.get("SYNC_DOCS_STATE_FILE", "/data/sync-docs-state.json")

SKIP_DIRS = {"_extra", "_images", "_includes", "_macros", "_video", "_workflows", "integrations"}
SKIP_FILES = {"docs/release-notes.md", "docs/release-notes/1-x.md"}
DOCS_BASE_URL = "https://docs.n8n.io"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def github_get(path):
    url = f"https://api.github.com/{path}"
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def should_include(filepath):
    parts = filepath.split("/")
    for part in parts:
        if part in SKIP_DIRS:
            return False
    if filepath in SKIP_FILES:
        return False
    return filepath.startswith("docs/") and filepath.endswith(".md")


def list_all_docs():
    data = github_get(f"repos/{REPO}/git/trees/main?recursive=1")
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


def fetch_file_content(filepath):
    data = github_get(f"repos/{REPO}/contents/{filepath}")
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8")
    return data.get("content", "")


def strip_frontmatter(content):
    if content.startswith("---"):
        try:
            end = content.index("---", 3)
            return content[end + 3:].strip()
        except ValueError:
            pass
    return content


def format_doc(filepath, content):
    content = strip_frontmatter(content)
    if len(content) < 50:
        return None
    rel = filepath.replace("docs/", "", 1)
    section = rel.split("/")[0] if "/" in rel else "general"
    slug = rel.replace(".md", "").replace("/index", "")
    url = f"{DOCS_BASE_URL}/{slug}/"
    return {
        "content": content,
        "context": f"n8n official documentation - {slug} ({url})",
        "tags": ["type:docs", "source:docs", f"section:{section}"],
        "metadata": {"url": url, "section": section, "filepath": filepath},
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
    full_run = "--full" in sys.argv
    dry_run = "--dry-run" in sys.argv
    test_limit = None
    if "--test" in sys.argv:
        idx = sys.argv.index("--test")
        test_limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 5

    if not HINDSIGHT_KEY:
        print("ERROR: HINDSIGHT_API_TENANT_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    sync_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if full_run or not state.get("last_sync"):
        print("Full scan via tree API...", flush=True)
        files = list_all_docs()
    else:
        print(f"Incremental since {state['last_sync']}...", flush=True)
        files = get_changed_files(state["last_sync"])

    print(f"Files to sync: {len(files)}", flush=True)

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
        item = format_doc(filepath, content)
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
