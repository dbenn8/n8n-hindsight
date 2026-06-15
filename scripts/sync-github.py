#!/usr/bin/env python3
"""
Incremental sync of n8n GitHub issues and PRs to Hindsight.

Tracks last successful sync timestamp in a state file. Only fetches
items created or updated since the last sync.

Usage:
    python3 sync-github.py                  # normal run
    python3 sync-github.py --full           # ignore state, fetch everything
    python3 sync-github.py --dry-run        # fetch and filter but don't retain

Env vars:
    GITHUB_TOKEN              — GitHub personal access token (optional but recommended for rate limits)
    HINDSIGHT_URL             — Hindsight API URL (default: http://127.0.0.1:8889 for in-container, or public URL)
    HINDSIGHT_API_TENANT_API_KEY — Hindsight API key
    SYNC_STATE_FILE           — path to state file (default: /data/sync-state.json)
"""
import asyncio
import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone

import sync_common

# Node detection for node:X tagging — the SINGLE canonical detector, vendored
# byte-identical from n8n-knowledge (scripts/lib/node_lookup.py; see its header
# for the do-not-fork / parity rule). community_tag() produces the exact same
# node:<tag> string that the plugin's do_gotcha_recall QUERIES, so recall finds
# what we WRITE. Best-effort: if the import fails, the sync still runs without
# node tags rather than breaking ingestion.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from node_lookup import identify_nodes, community_tag
    # Force the lazy node_lookup_data.json load NOW so a missing/corrupt data
    # file fails here (caught -> tags skipped) instead of mid-sync inside
    # format_item, which would abort the whole run.
    identify_nodes("warmup")
    _NODE_DETECT = True
except Exception as _e:  # pragma: no cover - defensive
    print(f"  WARN: node_lookup unavailable, skipping node:X tags ({_e})", file=sys.stderr)
    _NODE_DETECT = False

# Engagement floor: only node-tag items with real community engagement so
# low-signal noise isn't promoted in gotcha recall. engagement = reactions_total
# + comments*4 (matches format_results.py). Dan set 5 on 2026-06-15 — one-line
# knob, tune/roll back freely.
RETAIN_ENGAGEMENT_FLOOR = 5
MAX_NODE_TAGS = 5  # cap per item — avoid tag spam on issues that name many nodes

# JS-error phrasings whose embedded node-name tokens are noise, not the subject
# node. "X is not a function" carries the word "function" -> the deprecated
# Function node, which floods node:function across unrelated error-titled issues.
# Strip the PHRASE here (ingest only) rather than demoting the word in the shared
# detector — real titles like "Function node throws error" still detect it, and
# the recall/prompt side never sees this phrasing. Add patterns as needed.
_TITLE_NOISE_PATTERNS = [
    re.compile(r"\bis\s+not\s+a\s+function\b", re.IGNORECASE),
]


def _strip_title_noise(title):
    for pat in _TITLE_NOISE_PATTERNS:
        title = pat.sub(" ", title)
    return title

REPO = "n8n-io/n8n"
BANK_ID = sync_common.BANK_ID

HINDSIGHT_URL, HINDSIGHT_KEY = sync_common.resolve_env()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
STATE_FILE = os.environ.get("SYNC_STATE_FILE", "/data/sync-state.json")


def detect_node_tags(title, reactions_total, comments):
    """Return engagement-gated node:X tags for an item.

    Detect on the TITLE ONLY. A real dry-run (2026-06-15) showed issue BODIES are
    too noisy to tag from: users paste whole workflows, stack traces and node
    lists, so body-detection over-tags (e.g. a Wait-node bug whose body lists
    asana/postgres/github crowded out node:wait under the cap). The title
    reliably names the subject node ("Wait node hangs…", "OpenAI credential
    fails…"). Precision over recall: a mis-tag re-adds the cross-node noise this
    effort removes, while a vague-title miss still gets semantic (Tier 2) recall.

    Empty when detection is unavailable, engagement is below the floor, or no
    node is confidently detected. The same node_lookup the plugin uses, so the
    tags written here match the tags recall queries (the tag<->query contract).
    """
    if not _NODE_DETECT:
        return []
    if reactions_total + comments * 4 < RETAIN_ENGAGEMENT_FLOOR:
        return []
    seen, tags = set(), []
    for _, node_type in identify_nodes(_strip_title_noise(title)):
        tag = community_tag(node_type)
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(f"node:{tag}")
            if len(tags) >= MAX_NODE_TAGS:
                break
    return tags


def load_state():
    return sync_common.load_state(STATE_FILE)


def save_state(state):
    sync_common.save_state(STATE_FILE, state)


def github_api(path, params=None):
    """Call GitHub REST API with pagination."""
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    results = []
    url = f"https://api.github.com/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    while url:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)

            link = resp.headers.get("Link", "")
            url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split("<")[1].split(">")[0]
                    break

    return results


def fetch_issues(since=None):
    """Fetch open issues (not PRs) updated since timestamp."""
    print(f"Fetching issues{f' since {since}' if since else ' (full)'}...")
    params = {"state": "open", "per_page": "100", "sort": "updated", "direction": "asc"}
    if since:
        params["since"] = since

    all_items = github_api(f"repos/{REPO}/issues", params)
    issues = [i for i in all_items if "pull_request" not in i]
    print(f"  Fetched {len(issues)} issues")
    return issues


def fetch_issues_by_state(state, since=None):
    """Fetch issues by state, optionally filtered by since timestamp."""
    print(f"Fetching {state} issues{f' since {since}' if since else ''}...")
    params = {"state": state, "per_page": "100", "sort": "updated", "direction": "desc"}
    if since:
        params["since"] = since
    all_items = github_api(f"repos/{REPO}/issues", params)
    issues = [i for i in all_items if "pull_request" not in i]
    print(f"  Fetched {len(issues)} {state} issues")
    return issues


def fetch_closed_issues(target_total, open_count):
    """Fetch newest closed issues page-by-page, following cursor-based
    pagination via Link headers. Stops when we have enough issues."""
    remaining = target_total - open_count
    if remaining <= 0:
        return []
    print(f"Fetching up to {remaining} closed issues...")
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    issues = []
    url = f"https://api.github.com/repos/{REPO}/issues?" + urllib.parse.urlencode({
        "state": "closed", "per_page": "100", "sort": "updated", "direction": "desc",
    })
    pages = 0
    while url and len(issues) < remaining:
        batch = None
        link = ""
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    batch = json.loads(resp.read())
                    link = resp.headers.get("Link", "")
                break
            except Exception as e:
                if attempt < 2:
                    import time
                    time.sleep(2)
                    print(f"  Retry {attempt + 1} for page {pages + 1}: {e}")
                else:
                    print(f"  Failed after 3 attempts on page {pages + 1}: {e}")
                    return issues[:remaining]
        pages += 1
        for item in batch:
            if "pull_request" not in item:
                issues.append(item)
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split("<")[1].split(">")[0]
                break
        if pages % 20 == 0:
            print(f"  ...{len(issues)} closed issues so far (page {pages})")
    result = issues[:remaining]
    print(f"  Fetched {len(result)} closed issues ({pages} pages)")
    return result


def fetch_prs(since=None):
    """Fetch open PRs (with descriptions) updated since timestamp.

    Source PRs from the /issues endpoint, NOT /pulls. The /pulls LIST
    representation omits `comments` and `reactions`, which silently zeroed every
    PR's engagement and made it fail the node-tagging floor (a PR could never be
    node-tagged). The /issues representation of a PR (it carries a
    `pull_request` key) DOES include `comments` and `reactions`, so engagement is
    accurate. /issues also honors `since` server-side.
    """
    print(f"Fetching PRs{f' since {since}' if since else ' (full)'}...")
    params = {"state": "open", "per_page": "100", "sort": "updated", "direction": "desc"}
    if since:
        params["since"] = since

    all_items = github_api(f"repos/{REPO}/issues", params)
    prs = []
    for it in all_items:
        if "pull_request" not in it:  # keep only PRs; plain issues are handled elsewhere
            continue
        if not it.get("body"):
            continue
        prs.append(it)
    print(f"  Fetched {len(prs)} PRs")
    return prs


def format_item(item, item_type):
    number = item["number"]
    title = item["title"]
    body = item.get("body") or ""
    url = item["html_url"]
    labels = [l["name"] for l in item.get("labels", [])]
    created = item.get("created_at", "")
    reactions = item.get("reactions", {})
    state = item.get("state", "open")

    content = f"GitHub {item_type} #{number}: {title}\n\n{body}".strip()

    context = f"github {item_type} #{number} - {title} ({url})"
    tags = [f"type:github-{item_type}", f"source:github-{item_type}s", "pipeline:doc_id"]
    for label in labels:
        tags.append(f"label:{label}")
    if state == "closed":
        tags.append("state:closed")

    # Engagement-gated node:X tags so gotcha recall can find this item by node.
    # Detect on the title only (bodies are too noisy — see detect_node_tags).
    tags.extend(detect_node_tags(
        title,
        int(reactions.get("total_count", 0) or 0),
        int(item.get("comments", 0) or 0),
    ))

    metadata = {
        "url": url,
        "number": str(number),
        "created_at": created,
        "reactions_total": str(reactions.get("total_count", 0)),
        "of_those_plus1": str(reactions.get("+1", 0)),
        "comments": str(item.get("comments", 0)),
        "state": state,
        "author_association": item.get("author_association", "NONE"),
    }
    if item.get("state_reason"):
        metadata["state_reason"] = item["state_reason"]
    if item.get("closed_at"):
        metadata["closed_at"] = item["closed_at"]

    return {
        "document_id": f"github-{item_type}-{number}",
        "content": content,
        "context": context,
        "tags": tags,
        "metadata": metadata,
    }


def retain_batch(items):
    """Retain a batch of items to Hindsight via urllib (no aiohttp dependency)."""
    # flush=False preserves this script's original stderr behavior (the other
    # sync scripts flush their RETAIN ERROR line; sync-github historically did
    # not).
    return sync_common.retain_batch(
        items, HINDSIGHT_URL, HINDSIGHT_KEY, BANK_ID, flush=False
    )


def ingest(formatted_items):
    batch_size = sync_common.BATCH_SIZE
    success = 0
    failed = 0

    for i in range(0, len(formatted_items), batch_size):
        batch = formatted_items[i:i + batch_size]
        ok = retain_batch(batch)
        if ok:
            success += len(batch)
        else:
            failed += len(batch)
        if (success + failed) % 50 == 0 or (success + failed) == len(formatted_items):
            print(f"  [{success + failed}/{len(formatted_items)}] {success} ok, {failed} failed")

    return success, failed


def load_exclude_set(path):
    """Load a JSON file of issue/PR numbers to skip. Expects {"issues": [...], "prs": [...]}."""
    if not path or not os.path.exists(path):
        return set(), set()
    with open(path) as f:
        data = json.load(f)
    return set(data.get("issues", [])), set(data.get("prs", []))


def main():
    parser = sync_common.build_arg_parser(test=False)
    parser.add_argument("--exclude-file", default=None)
    args = parser.parse_known_args()[0]
    full_run = args.full
    dry_run = args.dry_run
    exclude_file = args.exclude_file

    if not HINDSIGHT_KEY:
        print("ERROR: HINDSIGHT_API_TENANT_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    exclude_issues, exclude_prs = load_exclude_set(exclude_file)
    if exclude_issues or exclude_prs:
        print(f"Excluding {len(exclude_issues)} issues + {len(exclude_prs)} PRs from retain")

    state = load_state()
    since = None if full_run else state.get("last_sync")
    sync_start = datetime.now(timezone.utc).isoformat()

    # Fetch
    TARGET_TOTAL = 4500
    issues = fetch_issues(since)
    if since:
        recently_closed = fetch_issues_by_state("closed", since)
        closed_issues = recently_closed
    else:
        closed_issues = fetch_closed_issues(TARGET_TOTAL, len(issues))
    prs = fetch_prs(since)

    all_issues = issues + closed_issues
    print(f"\nTotal: {len(issues)} open + {len(closed_issues)} closed issues, {len(prs)} PRs")

    # Format, skipping excluded numbers
    formatted = []
    skipped = 0
    for issue in all_issues:
        if issue["number"] in exclude_issues:
            skipped += 1
            continue
        formatted.append(format_item(issue, "issue"))
    for pr in prs:
        if pr["number"] in exclude_prs:
            skipped += 1
            continue
        formatted.append(format_item(pr, "pr"))

    if skipped:
        print(f"Skipped (in exclude list): {skipped}")
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
    success, failed = ingest(formatted)

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
    main()
