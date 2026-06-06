#!/usr/bin/env python3
"""
Sync n8n node specifications to Hindsight from n8n-mcp's nodes.db.

Extracts node specs from the SQLite database, splits large multi-resource
nodes into per-(resource, operation) units, and retains them with the
node_spec strategy.

Usage:
    python3 sync-nodes.py --db /path/to/nodes.db          # incremental
    python3 sync-nodes.py --db /path/to/nodes.db --full    # re-ingest all
    python3 sync-nodes.py --db /path/to/nodes.db --dry-run # show what would be synced
    python3 sync-nodes.py --db /path/to/nodes.db --test N  # sync only N nodes
    python3 sync-nodes.py --db /path/to/nodes.db --refresh-lookup out.json  # also regenerate lookup dict
    python3 sync-nodes.py --db /path/to/nodes.db --refresh-lookup out.json  # standalone (no ingestion)

If --db is not provided, extracts nodes.db from npm package:
    cd /tmp && npm pack n8n-mcp@latest && tar xzf n8n-mcp-*.tgz package/data/nodes.db

State tracked in STATE_FILE (default: /data/sync-nodes-state.json).
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
BANK_ID = "n8n"
STATE_FILE = os.environ.get("STATE_FILE", "/data/sync-nodes-state.json")

SPLIT_THRESHOLD = 12000
PROPERTIES_JSON_CAP = 9000
BATCH_SIZE = 5


# --- State management ---

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# --- Hindsight API ---

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


# --- Integration service name ---

def derive_service(node_type):
    """Derive integration service name from node_type.

    Examples:
        n8n-nodes-base.slack -> slack
        n8n-nodes-base.googleSheets -> googleSheets
        @n8n/n8n-nodes-langchain.agent -> agent
    """
    # Take the last segment after the final dot
    parts = node_type.rsplit(".", 1)
    if len(parts) == 2:
        return parts[1]
    return node_type


def sanitize_tag_value(value):
    """Clean a value for use in a tag (lowercase, no spaces, safe chars only)."""
    return re.sub(r"[^a-zA-Z0-9._-]", "", value.lower())


# --- Properties schema parsing ---

def find_property_by_name(properties, name):
    """Find a property object by its 'name' field in a properties list."""
    if not isinstance(properties, list):
        return None
    for prop in properties:
        if isinstance(prop, dict) and prop.get("name") == name:
            return prop
    return None


def get_resource_options(resource_prop):
    """Extract resource option values from a resource property."""
    if not resource_prop:
        return []
    options = resource_prop.get("options", [])
    return [opt.get("value", opt.get("name", "")) for opt in options if isinstance(opt, dict)]


def get_operation_options(operation_prop):
    """Extract operation option values from an operation property."""
    if not operation_prop:
        return []
    options = operation_prop.get("options", [])
    return [opt.get("value", opt.get("name", "")) for opt in options if isinstance(opt, dict)]


def get_operation_description(operation_prop, operation_value):
    """Get the human-readable description/name for an operation value."""
    if not operation_prop:
        return operation_value
    for opt in operation_prop.get("options", []):
        if isinstance(opt, dict) and opt.get("value") == operation_value:
            return opt.get("description", opt.get("name", operation_value))
    return operation_value


def get_resource_display_name(resource_prop, resource_value):
    """Get the human-readable name for a resource value."""
    if not resource_prop:
        return resource_value
    for opt in resource_prop.get("options", []):
        if isinstance(opt, dict) and opt.get("value") == resource_value:
            return opt.get("name", resource_value)
    return resource_value


def property_applies_to(prop, resource, operation):
    """Check if a property applies to a given resource+operation via displayOptions."""
    display_opts = prop.get("displayOptions", {})
    if not display_opts:
        # No displayOptions means it applies everywhere
        return True

    show = display_opts.get("show", {})
    hide = display_opts.get("hide", {})

    # Check 'show' conditions — property shown only when ALL conditions match
    if show:
        # Check resource condition
        res_cond = show.get("resource", show.get("/resource"))
        if res_cond and resource not in res_cond:
            return False
        # Check operation condition
        op_cond = show.get("operation", show.get("/operation"))
        if op_cond and operation not in op_cond:
            return False

    # Check 'hide' conditions — property hidden when ANY condition matches
    if hide:
        res_cond = hide.get("resource", hide.get("/resource"))
        if res_cond and resource in res_cond:
            return False
        op_cond = hide.get("operation", hide.get("/operation"))
        if op_cond and operation in op_cond:
            return False

    return True


def is_multi_resource(properties):
    """Check if a node has multiple resources defined."""
    resource_prop = find_property_by_name(properties, "resource")
    if not resource_prop:
        return False
    options = resource_prop.get("options", [])
    return len(options) > 1


def format_field_entry(prop, depth=0):
    """Format a single property as a human-readable field entry."""
    name = prop.get("name", "unknown")
    ptype = prop.get("type", "unknown")
    required = prop.get("required", False)
    description = prop.get("description", "")
    default = prop.get("default")

    req_label = "required" if required else "optional"
    entry = f"{'  ' * depth}{name} ({ptype}, {req_label})"
    if description:
        # Truncate long descriptions
        desc = description[:120]
        if len(description) > 120:
            desc += "..."
        entry += f" - {desc}"
    if default is not None and default != "" and default != []:
        entry += f" [default: {default}]"
    return entry


def collect_fields_for_operation(properties, resource, operation):
    """Collect all fields that apply to a given resource+operation pair.

    Deduplicates by field name — when multiple properties share a name but
    differ only by a third displayOptions condition (e.g. engagement type),
    the first match is kept to avoid confusing duplicate entries."""
    fields = []
    seen_names = set()
    if not isinstance(properties, list):
        return fields

    for prop in properties:
        if not isinstance(prop, dict):
            continue
        name = prop.get("name", "")
        if name in ("resource", "operation"):
            continue
        if property_applies_to(prop, resource, operation):
            if name not in seen_names:
                fields.append(prop)
                seen_names.add(name)

    return fields


def build_properties_json_for_operation(properties, resource, operation):
    """Build a filtered properties JSON for a specific resource+operation."""
    fields = collect_fields_for_operation(properties, resource, operation)
    result = json.dumps(fields, separators=(",", ":"))
    if len(result) > PROPERTIES_JSON_CAP:
        result = result[:PROPERTIES_JSON_CAP] + "..."
    return result


# --- Node formatting ---

def format_split_unit(node_row, resource, operation, properties, resource_prop, operation_prop):
    """Format a single (resource, operation) unit from a multi-resource node."""
    node_type = node_row["node_type"]
    display_name = node_row["display_name"]
    category = node_row["category"] or ""
    is_trigger = node_row["is_trigger"]
    service = derive_service(node_type)

    resource_display = get_resource_display_name(resource_prop, resource)
    operation_desc = get_operation_description(operation_prop, operation)

    fields = collect_fields_for_operation(properties, resource, operation)

    # Build NL content
    trigger_label = " (trigger)" if is_trigger else ""
    content_lines = [
        f"{display_name} node{trigger_label} -- {resource_display}: {operation_desc}.",
    ]

    if node_row["description"]:
        content_lines.append(f"Node description: {node_row['description']}")

    if fields:
        content_lines.append(f"Fields ({len(fields)}):")
        for field in fields[:30]:  # Cap at 30 fields to keep content reasonable
            content_lines.append(f"  {format_field_entry(field)}")
        if len(fields) > 30:
            content_lines.append(f"  ... and {len(fields) - 30} more fields")

    content = "\n".join(content_lines)

    # Build document_id
    safe_resource = sanitize_tag_value(resource)
    safe_operation = sanitize_tag_value(operation)
    document_id = f"nodespec-{node_type}-{safe_resource}.{safe_operation}"

    # Build properties JSON for this operation
    properties_json = build_properties_json_for_operation(properties, resource, operation)

    # Build tags
    safe_service = sanitize_tag_value(service)
    safe_category = sanitize_tag_value(category) if category else ""
    tags = [
        "type:node-spec",
        "source:n8n-node-introspection",
        f"node:{node_type}",
        f"integration:{safe_service}",
        f"resource:{safe_resource}",
        f"operation:{safe_resource}.{safe_operation}",
    ]
    if safe_category:
        tags.append(f"nodeclass:{safe_category}")

    return {
        "document_id": document_id,
        "content": content,
        "context": f"n8n node specification: {display_name} - {resource_display}/{operation_desc}",
        "tags": tags,
        "metadata": {
            "properties_json": properties_json,
            "node_type": node_type,
            "resource": resource,
            "operation": operation,
            "display_name": display_name,
        },
        "strategy": "node_spec",
    }


def format_single_unit(node_row, properties):
    """Format a small/single-op node as a single unit."""
    node_type = node_row["node_type"]
    display_name = node_row["display_name"]
    category = node_row["category"] or ""
    is_trigger = node_row["is_trigger"]
    service = derive_service(node_type)

    trigger_label = " (trigger)" if is_trigger else ""
    content_lines = [
        f"{display_name} node{trigger_label}.",
    ]

    if node_row["description"]:
        content_lines.append(f"Description: {node_row['description']}")

    # List operations if an operation property exists
    if isinstance(properties, list):
        operation_prop = find_property_by_name(properties, "operation")
        if operation_prop:
            ops = get_operation_options(operation_prop)
            if ops:
                content_lines.append(f"Operations: {', '.join(ops)}")

        # List top-level fields (excluding resource/operation)
        top_fields = [
            p for p in properties
            if isinstance(p, dict) and p.get("name") not in ("resource", "operation")
        ]
        if top_fields:
            content_lines.append(f"Fields ({len(top_fields)}):")
            for field in top_fields[:30]:
                content_lines.append(f"  {format_field_entry(field)}")
            if len(top_fields) > 30:
                content_lines.append(f"  ... and {len(top_fields) - 30} more fields")

    content = "\n".join(content_lines)

    # Properties JSON
    properties_json = json.dumps(properties, separators=(",", ":")) if properties else "{}"
    if len(properties_json) > PROPERTIES_JSON_CAP:
        properties_json = properties_json[:PROPERTIES_JSON_CAP] + "..."

    document_id = f"nodespec-{node_type}"

    safe_service = sanitize_tag_value(service)
    safe_category = sanitize_tag_value(category) if category else ""
    tags = [
        "type:node-spec",
        "source:n8n-node-introspection",
        f"node:{node_type}",
        f"integration:{safe_service}",
    ]
    if safe_category:
        tags.append(f"nodeclass:{safe_category}")

    return {
        "document_id": document_id,
        "content": content,
        "context": f"n8n node specification: {display_name}",
        "tags": tags,
        "metadata": {
            "properties_json": properties_json,
            "node_type": node_type,
            "display_name": display_name,
        },
        "strategy": "node_spec",
    }


def find_all_properties_by_name(properties, name):
    """Find ALL property objects with a given name (not just the first)."""
    if not isinstance(properties, list):
        return []
    return [p for p in properties if isinstance(p, dict) and p.get("name") == name]


def get_operations_for_resource(operation_props, resource):
    """Get the operations that apply to a specific resource.

    n8n nodes can have multiple 'operation' properties, each scoped to a
    different resource via displayOptions.show.resource. This finds the
    right one and returns its options."""
    for op_prop in operation_props:
        do = op_prop.get("displayOptions", {})
        show = do.get("show", {})
        res_cond = show.get("resource", show.get("/resource"))
        if res_cond and resource in res_cond:
            return op_prop, get_operation_options(op_prop)
    # Fallback: if no resource-scoped operation prop, use the first one
    if operation_props:
        return operation_props[0], get_operation_options(operation_props[0])
    return None, []


def process_node(node_row):
    """Process a single node row into one or more retain units."""
    node_type = node_row["node_type"]
    schema_raw = node_row["properties_schema"] or "[]"

    try:
        properties = json.loads(schema_raw)
    except (json.JSONDecodeError, TypeError):
        properties = []

    units = []

    if is_multi_resource(properties) and len(schema_raw) > SPLIT_THRESHOLD:
        resource_prop = find_property_by_name(properties, "resource")
        operation_props = find_all_properties_by_name(properties, "operation")
        resources = get_resource_options(resource_prop)

        if resources and operation_props:
            for resource in resources:
                op_prop, ops = get_operations_for_resource(operation_props, resource)
                if not ops:
                    continue
                for operation in ops:
                    unit = format_split_unit(
                        node_row, resource, operation,
                        properties, resource_prop, op_prop,
                    )
                    units.append(unit)

            if not units:
                units.append(format_single_unit(node_row, properties))
        else:
            units.append(format_single_unit(node_row, properties))
    else:
        units.append(format_single_unit(node_row, properties))

    return units


# --- Database access ---

def load_nodes(db_path):
    """Load all nodes from the SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT node_type, display_name, description, is_trigger, "
        "properties_schema, category FROM nodes ORDER BY node_type"
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


# --- Lookup dictionary generation ---

def generate_lookup(db_path, output_path):
    """Generate node_lookup_data.json from nodes.db.

    Two-pass: non-triggers first (priority), triggers fill gaps.
    Prefers nodes-base over community packages."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    non_triggers = []
    triggers = []
    for row in conn.execute('SELECT node_type, display_name, is_trigger FROM nodes'):
        (triggers if row['is_trigger'] else non_triggers).append(dict(row))
    conn.close()

    non_triggers.sort(key=lambda r: (0 if r['node_type'].startswith('nodes-base.') else 1))
    triggers.sort(key=lambda r: (0 if r['node_type'].startswith('nodes-base.') else 1))

    entries = {}

    def add_entry(key, nt, overwrite=True):
        if not overwrite and key in entries:
            return
        if key in entries and entries[key].startswith('nodes-base.') and not nt.startswith('nodes-base.'):
            return
        entries[key] = nt

    for row in non_triggers:
        nt = row['node_type']
        dn = row['display_name'].lower().strip()
        raw_suffix = nt.split('.')[-1]
        suffix = raw_suffix.lower()
        add_entry(dn, nt)
        add_entry(suffix, nt)
        split = re.sub(r'([a-z])([A-Z])', r'\1 \2', raw_suffix).lower()
        if split != suffix:
            add_entry(split, nt)

    for row in triggers:
        nt = row['node_type']
        dn = row['display_name'].lower().strip()
        raw_suffix = nt.split('.')[-1]
        suffix = raw_suffix.lower()
        add_entry(dn, nt, overwrite=False)
        add_entry(suffix, nt, overwrite=False)
        split = re.sub(r'([a-z])([A-Z])', r'\1 \2', raw_suffix).lower()
        if split != suffix:
            add_entry(split, nt, overwrite=False)
        base = re.sub(r'trigger$', '', suffix)
        if base and base != suffix:
            add_entry(base, nt, overwrite=False)

    with open(output_path, 'w') as f:
        json.dump(entries, f, indent=0, sort_keys=True)

    print(f"Lookup dictionary: {len(entries)} entries -> {output_path}", flush=True)
    return len(entries)


# --- Main ---

def main():
    full_run = "--full" in sys.argv
    dry_run = "--dry-run" in sys.argv
    test_limit = None
    db_path = None
    refresh_lookup_path = None

    if "--test" in sys.argv:
        idx = sys.argv.index("--test")
        test_limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 5

    if "--db" in sys.argv:
        idx = sys.argv.index("--db")
        db_path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    if "--refresh-lookup" in sys.argv:
        idx = sys.argv.index("--refresh-lookup")
        refresh_lookup_path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    if not db_path:
        # Try default location from npm pack extraction
        default_path = "/tmp/package/data/nodes.db"
        if os.path.exists(default_path):
            db_path = default_path
        else:
            print("ERROR: --db <path> required or extract nodes.db to /tmp/package/data/nodes.db", file=sys.stderr)
            print("  cd /tmp && npm pack n8n-mcp@latest 2>/dev/null && tar xzf n8n-mcp-*.tgz package/data/nodes.db", file=sys.stderr)
            sys.exit(1)

    if not os.path.exists(db_path):
        print(f"ERROR: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    # --refresh-lookup can run standalone (no API key needed)
    if refresh_lookup_path and not dry_run and not full_run and not test_limit:
        generate_lookup(db_path, refresh_lookup_path)
        return

    if not dry_run and not HINDSIGHT_KEY:
        print("ERROR: HINDSIGHT_API_TENANT_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    sync_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Load all nodes from DB
    print(f"Loading nodes from {db_path}...", flush=True)
    nodes = load_nodes(db_path)
    print(f"Nodes in database: {len(nodes)}", flush=True)

    # Refresh lookup dictionary alongside ingestion if requested
    if refresh_lookup_path:
        generate_lookup(db_path, refresh_lookup_path)

    if test_limit:
        nodes = nodes[:test_limit]
        print(f"Test mode: {test_limit} nodes only", flush=True)

    # Process all nodes into units
    print("Processing nodes into retain units...", flush=True)
    all_units = []
    split_count = 0
    single_count = 0

    for node_row in nodes:
        units = process_node(node_row)
        if len(units) > 1:
            split_count += 1
        else:
            single_count += 1
        all_units.extend(units)

    print(f"Units to retain: {len(all_units)} ({split_count} nodes split, {single_count} kept whole)", flush=True)

    if dry_run:
        print("\n--- DRY RUN ---", flush=True)
        for unit in all_units[:50]:
            content_preview = unit["content"][:100].replace("\n", " ")
            meta_keys = sorted(unit.get("metadata", {}).keys())
            print(f"\n  doc_id:  {unit['document_id']}")
            print(f"  tags:    {', '.join(unit['tags'])}")
            print(f"  content: {content_preview}...")
            print(f"  meta:    {', '.join(meta_keys)}")
        if len(all_units) > 50:
            print(f"\n  ... and {len(all_units) - 50} more units")
        print(f"\n=== DRY RUN: {len(all_units)} units from {len(nodes)} nodes ===", flush=True)
        return

    # Incremental: skip if last sync db hash matches (simple check via node count + mtime)
    if not full_run:
        db_stat = os.stat(db_path)
        db_fingerprint = f"{len(nodes)}:{int(db_stat.st_mtime)}"
        last_fingerprint = state.get("db_fingerprint")
        if last_fingerprint == db_fingerprint:
            print(f"No change detected (fingerprint: {db_fingerprint}). Use --full to force.", flush=True)
            return

    # Retain all units in batches
    retained = 0
    failed = 0
    batch = []

    for i, unit in enumerate(all_units):
        batch.append(unit)

        if len(batch) >= BATCH_SIZE:
            if retain_batch(batch):
                retained += len(batch)
            else:
                failed += len(batch)
            batch = []
            time.sleep(0.1)

        if (i + 1) % 50 == 0:
            print(f"  [{i + 1}/{len(all_units)}] retained={retained} failed={failed}", flush=True)

    if batch:
        if retain_batch(batch):
            retained += len(batch)
        else:
            failed += len(batch)

    # Update state
    db_stat = os.stat(db_path)
    db_fingerprint = f"{len(nodes)}:{int(db_stat.st_mtime)}"
    state["last_sync"] = sync_start
    state["last_run"] = sync_start
    state["last_count"] = retained
    state["total_synced"] = state.get("total_synced", 0) + retained
    state["db_fingerprint"] = db_fingerprint
    state["nodes_in_db"] = len(nodes)
    state["units_generated"] = len(all_units)
    save_state(state)

    print(f"\n=== DONE: {retained} retained, {failed} failed ({len(all_units)} units from {len(nodes)} nodes) ===", flush=True)


if __name__ == "__main__":
    main()
