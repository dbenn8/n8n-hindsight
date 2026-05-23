#!/usr/bin/env python3
"""
Incremental sync of n8n GitHub issues and PRs to Hindsight.

Tracks last successful sync timestamp in a state file. Only fetches
items created or updated since the last sync.

Usage:
    python3 sync-github.py                  # normal run
    python3 sync-github.py --full           # ignore state, fetch everything
    python3 sync-github.py --dry-run        # fetch and filter but don't retain

State file: ~/.n8n-hindsight-sync-state.json
"""
import asyncio
import aiohttp
import json
import subprocess
import sys
import os
from datetime import datetime, timezone

HINDSIGHT_URL = "https://n8nhindsight.applikuapp.com"
HINDSIGHT_KEY = "4afd972990864781a845a5b17084b8ce75f2d5d2cab15a057d006a2ca0d18b8e"
BANK_ID = "n8n"
REPO = "n8n-io/n8n"
STATE_FILE = os.path.expanduser("~/.n8n-hindsight-sync-state.json")

HIGH_SIGNAL_LABELS = {"bug", "feature", "community", "Help Wanted", "type:bug", "type:enhancement", "bug-report"}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_issues(since=None):
    """Fetch open issues updated since timestamp."""
    print(f"Fetching issues{f' since {since}' if since else ' (full)'}...")
    cmd = [
        "gh", "api", f"repos/{REPO}/issues",
        "--paginate",
        "-q", '.[] | select(.pull_request == null) | {number, title, body: (.body // "" | .[0:4000]), labels: [.labels[].name], comments: .comments, url: .html_url, created_at: .created_at, updated_at: .updated_at}',
    ]
    if since:
        cmd[3] = f"repos/{REPO}/issues?state=open&since={since}&sort=updated&direction=asc"
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    issues = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            try:
                issues.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    print(f"  Fetched {len(issues)} issues")
    return issues


def fetch_prs(since=None):
    """Fetch open PRs updated since timestamp."""
    print(f"Fetching PRs{f' since {since}' if since else ' (full)'}...")
    # GitHub PRs API doesn't support 'since', so we fetch all and filter client-side
    cmd = [
        "gh", "api", f"repos/{REPO}/pulls?state=open&sort=updated&direction=desc",
        "--paginate",
        "-q", '.[] | select(.body != null and .body != "") | {number, title, body: (.body // "" | .[0:4000]), labels: [.labels[].name], comments: .comments, url: .html_url, created_at: .created_at, updated_at: .updated_at}',
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    prs = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            try:
                item = json.loads(line)
                if since and item.get("updated_at", "") <= since:
                    continue
                prs.append(item)
            except json.JSONDecodeError:
                continue
    print(f"  Fetched {len(prs)} PRs")
    return prs


def filter_high_signal(items, min_comments=2):
    filtered = []
    for item in items:
        labels = set(item.get("labels", []))
        comments = item.get("comments") or 0
        if labels & HIGH_SIGNAL_LABELS or comments >= min_comments:
            filtered.append(item)
    return filtered


def format_item(item, item_type):
    number = item["number"]
    title = item["title"]
    body = item.get("body", "")
    url = item["url"]
    labels = item.get("labels", [])
    created = item.get("created_at", "")

    content = f"GitHub {item_type} #{number}: {title}\n\n{body}".strip()
    if len(content) > 5000:
        content = content[:5000] + "..."

    context = f"github {item_type} #{number} - {title} ({url})"

    tags = [f"type:github-{item_type}", "source:github"]
    for label in labels:
        tags.append(f"label:{label}")

    return {
        "content": content,
        "context": context,
        "tags": tags,
        "metadata": {"url": url, "number": str(number), "created_at": created},
    }


async def retain_batch(session, items):
    payload = {"items": items, "async": True}
    headers = {
        "Authorization": f"Bearer {HINDSIGHT_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(
            f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/memories",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status in (200, 201, 202):
                return True
            else:
                text = await resp.text()
                print(f"  FAIL ({resp.status}): {text[:100]}", file=sys.stderr)
                return False
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False


async def ingest(formatted_items):
    batch_size = 5
    success = 0
    failed = 0

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(formatted_items), batch_size):
            batch = formatted_items[i:i + batch_size]
            ok = await retain_batch(session, batch)
            if ok:
                success += len(batch)
            else:
                failed += len(batch)
            if (success + failed) % 50 == 0 or (success + failed) == len(formatted_items):
                print(f"  [{success + failed}/{len(formatted_items)}] {success} ok, {failed} failed")

    return success, failed


async def main():
    full_run = "--full" in sys.argv
    dry_run = "--dry-run" in sys.argv

    state = load_state()
    since = None if full_run else state.get("last_sync")

    sync_start = datetime.now(timezone.utc).isoformat()

    # Fetch
    issues = fetch_issues(since)
    prs = fetch_prs(since)

    # Filter
    high_signal_issues = filter_high_signal(issues)
    high_signal_prs = filter_high_signal(prs, min_comments=1)

    print(f"\nHigh-signal: {len(high_signal_issues)} issues, {len(high_signal_prs)} PRs")
    print(f"Filtered out: {len(issues) - len(high_signal_issues)} issues, {len(prs) - len(high_signal_prs)} PRs")

    # Format
    formatted = []
    for issue in high_signal_issues:
        formatted.append(format_item(issue, "issue"))
    for pr in high_signal_prs:
        formatted.append(format_item(pr, "pr"))

    print(f"Total to ingest: {len(formatted)}")

    if dry_run:
        print("\n=== DRY RUN — skipping ingestion ===")
        return

    if not formatted:
        print("\nNothing new to ingest.")
        state["last_sync"] = sync_start
        state["last_run"] = sync_start
        state["last_count"] = 0
        save_state(state)
        return

    # Ingest
    print("\nIngesting into Hindsight...")
    success, failed = await ingest(formatted)

    # Update state only on success
    if failed == 0:
        state["last_sync"] = sync_start
        state["last_run"] = sync_start
        state["last_count"] = success
        state["total_synced"] = state.get("total_synced", 0) + success
        save_state(state)
        print(f"\nState saved. Next run will fetch items updated after {sync_start}")
    else:
        print(f"\n{failed} failures — state NOT updated (will retry on next run)")

    print(f"\n=== DONE: {success} ingested, {failed} failed ===")


if __name__ == "__main__":
    asyncio.run(main())
