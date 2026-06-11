# Workflow Validator API

The `ops-proxy` FastAPI service exposes a public n8n workflow validation endpoint.
It extracts an n8n workflow object from a request (or from raw model output),
validates it with the [`n8n-mcp`](https://www.npmjs.com/package/n8n-mcp) validator
engine, and returns structured validation results plus short, actionable repair
messages.

The endpoint is intended for consumers such as a Claude Code plugin or an
evaluation harness that need to check whether generated n8n workflow JSON is
valid before importing it.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/public/validate-workflow` | Validate a workflow or model response |
| `GET`  | `/public/validator-health` | Validator backend status and version metadata |

Both routes are served by the `ops-proxy` service and proxied through nginx.

## POST /public/validate-workflow

### Request

The endpoint accepts a JSON object in one of three shapes.

**1. Raw workflow JSON (body is the workflow itself)**

If the top-level body contains a `nodes` or `connections` key and does *not*
contain a `workflow` key, the whole body is treated as the workflow object.

```json
{
  "nodes": [],
  "connections": {}
}
```

**2. Wrapped workflow object**

```json
{
  "workflow": {
    "nodes": [],
    "connections": {}
  },
  "max_errors": 8
}
```

**3. Full model response text**

The server extracts the workflow JSON from the text — first from fenced
```` ```json ```` blocks, then from brace-balanced JSON embedded in prose.

```json
{
  "response_text": "Here is the workflow...```json\n{\"nodes\":[],\"connections\":{}}\n```"
}
```

### Request fields

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `workflow` | object | — | An n8n workflow object. Must be a JSON object if present. |
| `response_text` | string | — | Full model output to extract a workflow from. |
| `max_errors` | integer | `8` | Cap on `repair_messages`. Must be between 1 and 50. |
| `debug` | boolean | `false` | When `true`, the response echoes the parsed `workflow`. |

You must provide either `workflow` or a non-empty `response_text` (the raw-JSON
body form satisfies this by acting as `workflow`). Unknown fields are ignored.

### Response

Validation always returns `200 OK` — including for invalid workflows. Non-2xx
status codes are reserved for malformed requests and service failures.

When a workflow is found and validated:

```json
{
  "valid": false,
  "has_json": true,
  "extract_error": null,
  "error_count": 2,
  "warning_count": 1,
  "node_count": 4,
  "trigger_count": 1,
  "repair_messages": [
    "Required property 'Name' cannot be empty",
    "Expression format error in node Broken"
  ],
  "feedback_block": "- Required property 'Name' cannot be empty\n- Expression format error in node Broken",
  "errors": [
    {
      "type": "schema_error",
      "message": "Required property 'Name' cannot be empty",
      "node": "Broken"
    }
  ],
  "warnings": [],
  "statistics": {
    "totalNodes": 4,
    "triggerNodes": 1
  },
  "suggestions": [],
  "validator_info": {
    "validator_engine": "n8n-mcp",
    "configured_n8n_mcp_version": "2.57.3",
    "installed_n8n_mcp_version": "2.57.3",
    "nodes_db_sha256": "...",
    "nodes_content_sha256": "..."
  }
}
```

When no workflow can be extracted from `response_text`:

```json
{
  "valid": false,
  "has_json": false,
  "extract_error": "no_json_found",
  "error_count": 1,
  "warning_count": 0,
  "node_count": 0,
  "trigger_count": 0,
  "repair_messages": [
    "Return a single complete importable n8n workflow JSON object inside a ```json code block.",
    "Include both a 'nodes' array and a 'connections' object."
  ],
  "feedback_block": "- Return a single complete importable n8n workflow JSON object inside a ```json code block.\n- Include both a 'nodes' array and a 'connections' object.",
  "errors": [{ "type": "extract_error", "message": "no_json_found" }],
  "warnings": [],
  "statistics": { "totalNodes": 0, "triggerNodes": 0 },
  "suggestions": []
}
```

### Response fields

| Field | Type | Notes |
|-------|------|-------|
| `valid` | boolean | Whether the workflow passed validation. |
| `has_json` | boolean | Whether a workflow object was found. |
| `extract_error` | string \| null | `"no_json_found"` when text contained no extractable workflow; otherwise `null`. |
| `error_count` | integer | Number of validator errors. |
| `warning_count` | integer | Number of validator warnings. |
| `node_count` | integer | Total nodes (from validator statistics, else from the workflow). |
| `trigger_count` | integer | Number of trigger nodes. |
| `repair_messages` | string[] | Deduped, whitespace-normalized error messages, capped at `max_errors`. |
| `feedback_block` | string | `repair_messages` rendered as a `- `-prefixed bullet list. |
| `errors` | object[] | Raw validator error objects, passed through unchanged. |
| `warnings` | object[] | Raw validator warning objects, passed through unchanged. |
| `statistics` | object | Raw validator statistics (e.g. `totalNodes`, `triggerNodes`). |
| `suggestions` | array | Raw validator suggestions. |
| `validator_info` | object | Validator engine and version metadata (see below). |
| `workflow` | object | Only present when the request set `debug: true`. |

Enrichment beyond `repair_messages` / `feedback_block` — for example, building a
full repair prompt or running an automatic repair loop — is intentionally left
to the client. The server is a thin transport over the raw validator plus a
small, deterministic message summary.

### Error responses

| Status | Condition |
|--------|-----------|
| `400` | Malformed JSON body, or a request field fails validation (e.g. `max_errors` out of range, `workflow` not an object). |
| `413` | Request body exceeds the size cap. |
| `429` | Rate limit exceeded. |
| `503` | Validator backend is unavailable (e.g. Node.js missing, validator timed out, or forward target unreachable). |

## GET /public/validator-health

Returns the active validator backend and its version metadata. Use this to
confirm the validator is ready (and which `n8n-mcp` version / node database is
in use) before running large validation batches.

In local mode:

```json
{
  "status": "ok",
  "validator_mode": "local",
  "validator_info": {
    "validator_engine": "n8n-mcp",
    "configured_n8n_mcp_version": "2.57.3",
    "installed_n8n_mcp_version": "2.57.3",
    "nodes_db_sha256": "...",
    "nodes_content_sha256": "..."
  }
}
```

`status` becomes `"degraded"` if the validator failed to start. In forward mode
(see below) the payload reports `validator_mode: "forward"`, the configured
`validator_forward_url`, and the downstream validator's `validator_info` rather
than the local app's own metadata.

The same payload is also served at `GET /health`.

### `validator_info` fields

| Field | Notes |
|-------|-------|
| `validator_engine` | Always `"n8n-mcp"`. |
| `configured_n8n_mcp_version` | `n8n-mcp` version pinned in `ops-proxy/package.json`. |
| `installed_n8n_mcp_version` | Version actually resolved at runtime. |
| `nodes_db_sha256` | SHA-256 of the `n8n-mcp` `nodes.db` file. |
| `nodes_content_sha256` | Stable content hash of the `nodes` table rows (robust to in-place SQLite churn). |

## Configuration

The service reads the following environment variables.

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKFLOW_VALIDATOR_FORWARD_URL` | — | If set, the endpoint forwards each validation request to this URL instead of validating locally (forward mode). Health checks proxy to the same host's `/health`. |
| `WORKFLOW_VALIDATOR_FORWARD_TIMEOUT_SECONDS` | `30` | Timeout for forwarded validation requests (minimum 1). |
| `WORKFLOW_VALIDATOR_MAX_BODY_BYTES` | `1048576` (1 MiB) | Maximum accepted request body size; requests over the cap return `413`. Minimum 1024. |
| `N8N_MCP_INSTALL_ROOT` | — | Optional explicit path to the installed `n8n-mcp` package (otherwise resolved from `node_modules`). |

### Forward mode

When `WORKFLOW_VALIDATOR_FORWARD_URL` is set, the local validator process is not
started. Each `POST /public/validate-workflow` request body is forwarded
verbatim to the configured URL, and the downstream response (status code and
body) is returned unchanged. This lets the public-facing app delegate validation
to a separate dedicated validator deployment while keeping a single public
contract.

## Rate limit

`POST /public/validate-workflow` is rate limited to **30 requests per minute**
per client. Exceeding the limit returns `429` with `{"detail": "Rate limit exceeded"}`.

## Example

```bash
curl -sS -X POST "https://YOUR-HOST/public/validate-workflow" \
  -H "Content-Type: application/json" \
  -d '{
    "response_text": "```json\n{\"nodes\":[],\"connections\":{}}\n```",
    "max_errors": 8
  }'
```

## Implementation

| Component | File |
|-----------|------|
| FastAPI endpoints, forwarding, rate limit, body cap | `ops-proxy/app.py` |
| Extraction, validation, summarization, response shaping | `ops-proxy/workflow_validator.py` |
| Node bridge into the `n8n-mcp` validator | `ops-proxy/validator_bridge.js` |
| Pinned `n8n-mcp` dependency | `ops-proxy/package.json` |

Tests live in `ops-proxy/tests/` (`pytest`), covering valid and invalid
workflows, deduped repair messages, `no_json_found` handling, fenced and bare
JSON extraction, body-size and malformed-JSON rejection, and forward mode.
