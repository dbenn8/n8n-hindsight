#!/usr/bin/env python3
"""
Sync n8n release notes to Hindsight via GitHub API.

Usage:
    python3 sync-releases.py              # incremental (only new releases)
    python3 sync-releases.py --full       # re-ingest all releases
    python3 sync-releases.py --dry-run    # show what would be synced
    python3 sync-releases.py --test N     # sync only N releases (for testing)

State tracked in SYNC_STATE_FILE (default: /data/sync-releases-state.json).
"""
import json
import os
import sys
import time
import urllib.request

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
BANK_ID = "n8n"
REPO = "n8n-io/n8n"
STATE_FILE = os.environ.get("SYNC_RELEASES_STATE_FILE", "/data/sync-releases-state.json")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def github_get(url):
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
        link = resp.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split("<")[1].split(">")[0]
                break
        return data, next_url


def fetch_all_releases():
    """Fetch all releases, following pagination."""
    all_releases = []
    url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
    while url:
        batch, next_url = github_get(url)
        all_releases.extend(batch)
        url = next_url
        if url:
            print(f"  ...{len(all_releases)} releases fetched", flush=True)
    return all_releases


def is_version_release(release):
    """Filter to only n8n@X.Y.Z releases, skip beta/stable pointer tags."""
    tag = release.get("tag_name", "")
    return tag.startswith("n8n@") and "-exp." not in tag


def format_release(release):
    tag = release["tag_name"]
    version = tag.replace("n8n@", "")
    body = release.get("body") or ""
    if len(body) < 10:
        return None
    published = release.get("published_at", "")
    url = release["html_url"]

    is_v1 = version.startswith("1.")
    branch = "v1 (legacy)" if is_v1 else "v2 (current)"

    return {
        "document_id": f"release-{version}",
        "content": f"n8n Release {version}\nPublished: {published[:10]}\nBranch: {branch}\n\n{body}",
        "context": f"n8n release notes - {version} ({url})",
        "tags": ["type:release-notes", "source:github-releases", f"version:{version}", "pipeline:doc_id"],
        "metadata": {
            "url": url,
            "version": version,
            "published_at": published,
            "branch": branch,
        },
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
    last_sync = None if full_run else state.get("last_sync")

    print("Fetching releases from GitHub API...", flush=True)
    all_releases = fetch_all_releases()
    print(f"Total releases: {len(all_releases)}", flush=True)

    # Filter to version releases only
    releases = [r for r in all_releases if is_version_release(r)]
    print(f"Version releases (excl beta/stable/exp): {len(releases)}", flush=True)

    # Incremental: only releases published after last_sync
    if last_sync:
        releases = [r for r in releases if r.get("published_at", "") > last_sync]
        print(f"New since {last_sync}: {len(releases)}", flush=True)

    if test_limit:
        releases = releases[:test_limit]
        print(f"Test mode: {test_limit} releases only", flush=True)

    if dry_run:
        for r in releases[:20]:
            body_len = len(r.get("body", "") or "")
            print(f"  {r['tag_name']:25s} | {r['published_at'][:10]} | {body_len} chars")
        if len(releases) > 20:
            print(f"  ... and {len(releases) - 20} more")
        print(f"\n=== DRY RUN: {len(releases)} releases ===", flush=True)
        return

    retained = 0
    skipped = 0
    failed = 0
    batch = []

    for i, release in enumerate(releases):
        item = format_release(release)
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
            print(f"  [{i + 1}/{len(releases)}] retained={retained} skipped={skipped} failed={failed}", flush=True)

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
