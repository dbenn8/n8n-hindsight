#!/usr/bin/env python3
"""
Sync n8n workflow JSON examples to Hindsight.

Reads workflow JSON files from a directory and breaks each one into:
  - Node units (one per node, with wiring context)
  - Topology unit (edge list for the whole workflow)
  - Source unit (full JSON for explicit retrieval)

Usage:
    python3 sync-workflows.py                          # incremental
    python3 sync-workflows.py --full                   # re-ingest all workflows
    python3 sync-workflows.py --dry-run                # show what would be synced
    python3 sync-workflows.py --test N                 # process only N workflow files
    python3 sync-workflows.py --dir /path/to/workflows # specify workflow directory

State tracked in STATE_FILE (default: /data/sync-workflows-state.json).
"""
import glob
import json
import os
import re
import sys
import time
import urllib.request

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
BANK_ID = "n8n"
STATE_FILE = os.environ.get("SYNC_WORKFLOWS_STATE_FILE",
    os.environ.get("STATE_FILE", "/data/sync-workflows-state.json"))

TRIGGER_KEYWORDS = ["trigger", "webhook", "cron", "schedule", "start", "emailimap"]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def slugify(text):
    """Create a URL-safe kebab-case slug from text."""
    s = text.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    return s


def is_trigger_node(node_type):
    """Check if a node type is a trigger."""
    lower = node_type.lower()
    return any(kw in lower for kw in TRIGGER_KEYWORDS)


def build_wiring_map(workflow):
    """Build inbound/outbound maps from the connections object."""
    nodes = workflow.get("nodes", [])
    connections = workflow.get("connections", {})

    # Map node names to their types
    name_to_type = {n["name"]: n.get("type", "unknown") for n in nodes}

    # Initialize maps
    inbound = {n["name"]: [] for n in nodes}
    outbound = {n["name"]: [] for n in nodes}

    for source_name, outputs in connections.items():
        if not isinstance(outputs, dict):
            continue
        for output_type, output_indices in outputs.items():
            if not isinstance(output_indices, list):
                continue
            for connections_list in output_indices:
                if not isinstance(connections_list, list):
                    continue
                for conn in connections_list:
                    if not isinstance(conn, dict):
                        continue
                    target_name = conn.get("node", "")
                    if target_name:
                        if target_name not in outbound.get(source_name, []):
                            outbound.setdefault(source_name, []).append(target_name)
                        if source_name not in inbound.get(target_name, []):
                            inbound.setdefault(target_name, []).append(source_name)

    return inbound, outbound


def build_edges(workflow):
    """Build a list of 'A -> B' edge strings from connections."""
    connections = workflow.get("connections", {})
    edges = []
    seen = set()

    for source_name, outputs in connections.items():
        if not isinstance(outputs, dict):
            continue
        for output_type, output_indices in outputs.items():
            if not isinstance(output_indices, list):
                continue
            for connections_list in output_indices:
                if not isinstance(connections_list, list):
                    continue
                for conn in connections_list:
                    if not isinstance(conn, dict):
                        continue
                    target_name = conn.get("node", "")
                    if target_name:
                        edge_key = f"{source_name} -> {target_name}"
                        if edge_key not in seen:
                            seen.add(edge_key)
                            edges.append(edge_key)

    return edges


def format_node_unit(node, wf_name, wf_slug, inbound, outbound, filepath):
    """Format a single node into a Hindsight memory item."""
    node_name = node.get("name", "Unnamed")
    node_type = node.get("type", "unknown")
    node_slug = slugify(node_name)
    params = node.get("parameters", {})

    receives = inbound.get(node_name, [])
    sends = outbound.get(node_name, [])

    receives_str = ", ".join(receives) if receives else "none (entry point)"
    sends_str = ", ".join(sends) if sends else "none (terminal)"

    params_json = json.dumps(params, indent=2)
    if len(params_json) > 2000:
        params_json = params_json[:2000] + "\n... [truncated]"

    content = (
        f"Node '{node_name}' (type {node_type}) in workflow '{wf_name}'. "
        f"Receives from: [{receives_str}]. Sends to: [{sends_str}]. "
        f"Config: {params_json}"
    )

    tags = [
        "type:workflow-node",
        "source:n8n-docs-workflows",
        f"wf:{wf_slug}",
        f"node:{node_type}",
    ]
    if is_trigger_node(node_type):
        tags.append(f"trigger:{node_type}")

    return {
        "document_id": f"wf-{wf_slug}-node-{node_slug}",
        "content": content,
        "context": f"Node '{node_name}' in n8n workflow '{wf_name}'",
        "tags": tags,
        "metadata": {
            "workflow_name": wf_name,
            "node_name": node_name,
            "node_type": node_type,
            "url": filepath,
        },
        "strategy": "workflow_json",
    }


def format_topo_unit(workflow, wf_name, wf_slug, edges):
    """Format the topology unit for a workflow."""
    nodes = workflow.get("nodes", [])
    node_types = list(set(n.get("type", "unknown") for n in nodes))

    edges_str = "; ".join(edges) if edges else "no connections"
    content = f"Topology of '{wf_name}': {edges_str}"

    tags = [
        "type:workflow-topo",
        "source:n8n-docs-workflows",
        f"wf:{wf_slug}",
    ]
    for nt in sorted(node_types):
        tags.append(f"node:{nt}")

    return {
        "document_id": f"wf-{wf_slug}-topo",
        "content": content,
        "context": f"Topology of n8n workflow '{wf_name}'",
        "tags": tags,
        "metadata": {
            "workflow_name": wf_name,
            "node_count": str(len(nodes)),
            "edge_count": str(len(edges)),
        },
        "strategy": "workflow_json",
    }


def format_source_unit(workflow, wf_name, wf_slug):
    """Format the full source JSON unit for a workflow."""
    nodes = workflow.get("nodes", [])
    content = json.dumps(workflow, indent=2)

    if len(content) > 50000:
        content = content[:50000] + "\n... [truncated]"

    return {
        "document_id": f"wf-{wf_slug}-source",
        "content": content,
        "context": f"Full JSON source of n8n workflow '{wf_name}'",
        "tags": [
            "type:workflow-source",
            "source:n8n-docs-workflows",
            f"wf:{wf_slug}",
        ],
        "metadata": {
            "workflow_name": wf_name,
            "node_count": str(len(nodes)),
        },
        "strategy": "workflow_json",
    }


def process_workflow(filepath):
    """Process a single workflow JSON file into memory items."""
    with open(filepath) as f:
        workflow = json.load(f)

    wf_name = workflow.get("name", os.path.basename(filepath).replace(".json", ""))
    wf_slug = slugify(wf_name)
    nodes = workflow.get("nodes", [])

    if not nodes:
        return []

    inbound, outbound = build_wiring_map(workflow)
    edges = build_edges(workflow)

    items = []

    # Node units
    for node in nodes:
        item = format_node_unit(node, wf_name, wf_slug, inbound, outbound, filepath)
        items.append(item)

    # Topology unit
    items.append(format_topo_unit(workflow, wf_name, wf_slug, edges))

    # Source unit
    items.append(format_source_unit(workflow, wf_name, wf_slug))

    return items


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


def find_workflow_dir():
    """Find the default workflow directory relative to the script."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Check common locations
    candidates = [
        os.path.join(script_dir, "..", "docs", "_workflows"),
        os.path.join(script_dir, "..", "_workflows"),
        "/data/workflows",
    ]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    return None


def main():
    full_run = "--full" in sys.argv
    dry_run = "--dry-run" in sys.argv
    test_limit = None
    workflow_dir = None

    if "--test" in sys.argv:
        idx = sys.argv.index("--test")
        test_limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 5

    if "--dir" in sys.argv:
        idx = sys.argv.index("--dir")
        if idx + 1 < len(sys.argv):
            workflow_dir = sys.argv[idx + 1]
        else:
            print("ERROR: --dir requires a path argument", file=sys.stderr)
            sys.exit(1)

    if not workflow_dir:
        workflow_dir = find_workflow_dir()

    if not workflow_dir or not os.path.isdir(workflow_dir):
        print(f"ERROR: Workflow directory not found: {workflow_dir}", file=sys.stderr)
        print("Use --dir /path/to/workflows to specify the directory.", file=sys.stderr)
        sys.exit(1)

    if not dry_run and not HINDSIGHT_KEY:
        print("ERROR: HINDSIGHT_API_TENANT_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    # Find all JSON files in the workflow directory
    json_files = sorted(glob.glob(os.path.join(workflow_dir, "*.json")))
    if not json_files:
        # Also check subdirectories
        json_files = sorted(glob.glob(os.path.join(workflow_dir, "**", "*.json"), recursive=True))

    print(f"Workflow directory: {workflow_dir}", flush=True)
    print(f"JSON files found: {len(json_files)}", flush=True)

    if not json_files:
        print("No workflow JSON files found.", flush=True)
        return

    state = load_state()
    sync_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    last_sync = None if full_run else state.get("last_sync")

    # For incremental mode, filter to files modified since last sync
    if last_sync and not full_run:
        last_sync_ts = time.mktime(time.strptime(last_sync, "%Y-%m-%dT%H:%M:%SZ"))
        changed_files = []
        for f in json_files:
            if os.path.getmtime(f) > last_sync_ts:
                changed_files.append(f)
        print(f"Incremental: {len(changed_files)} files changed since {last_sync}", flush=True)
        json_files = changed_files

    if test_limit:
        json_files = json_files[:test_limit]
        print(f"Test mode: {test_limit} files only", flush=True)

    if not json_files:
        print("No files to process.", flush=True)
        return

    # Process all workflow files into items
    all_items = []
    files_processed = 0
    files_skipped = 0

    for filepath in json_files:
        try:
            items = process_workflow(filepath)
            if items:
                all_items.extend(items)
                files_processed += 1
            else:
                files_skipped += 1
        except Exception as e:
            print(f"  ERROR processing {os.path.basename(filepath)}: {e}", file=sys.stderr, flush=True)
            files_skipped += 1

    print(f"Workflows processed: {files_processed}, skipped: {files_skipped}", flush=True)
    print(f"Total units generated: {len(all_items)}", flush=True)

    if dry_run:
        for item in all_items:
            preview = item["content"][:100].replace("\n", " ")
            tags_str = ", ".join(item["tags"])
            print(f"  {item['document_id']}")
            print(f"    tags: {tags_str}")
            print(f"    content: {preview}...")
            print()
        print(f"\n=== DRY RUN: {len(all_items)} units from {files_processed} workflows ===", flush=True)
        return

    # Retain in batches
    retained = 0
    failed = 0
    batch = []

    for i, item in enumerate(all_items):
        batch.append(item)

        if len(batch) >= 5:
            if retain_batch(batch):
                retained += len(batch)
            else:
                failed += len(batch)
            batch = []
            time.sleep(0.1)

        if (i + 1) % 50 == 0:
            print(f"  [{i + 1}/{len(all_items)}] retained={retained} failed={failed}", flush=True)

    if batch:
        if retain_batch(batch):
            retained += len(batch)
        else:
            failed += len(batch)

    state["last_sync"] = sync_start
    state["last_run"] = sync_start
    state["last_count"] = retained
    state["workflows_processed"] = files_processed
    state["total_synced"] = state.get("total_synced", 0) + retained
    save_state(state)

    print(f"\n=== DONE: {retained} retained, {failed} failed ({files_processed} workflows) ===", flush=True)


if __name__ == "__main__":
    main()
