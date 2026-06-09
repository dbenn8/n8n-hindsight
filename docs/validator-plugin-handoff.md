# Workflow Validator Endpoint Handoff

This repo now exposes a server-side n8n workflow validator endpoint intended for the `n8n-knowledge` plugin and eval harness.

## Branch and Endpoint

- Branch: `n8n-validator`
- Endpoint: `POST /public/validate-workflow`

The route is proxied by nginx to the `ops-proxy` FastAPI service, which owns request parsing, extraction, response shaping, and repair-prompt generation.

## What The Plugin Can Send

### Option 1: full model response text

```json
{
  "response_text": "Here is the workflow...```json\n{\"nodes\":[],\"connections\":{}}\n```",
  "original_prompt": "Build an n8n workflow that posts to Slack when a webhook fires",
  "max_errors": 8,
  "include_repair_prompt": true
}
```

### Option 2: raw workflow object

```json
{
  "workflow": {
    "nodes": [],
    "connections": {}
  },
  "original_prompt": "Build an n8n workflow that posts to Slack when a webhook fires",
  "max_errors": 8,
  "include_repair_prompt": true
}
```

### Option 3: raw workflow JSON body directly

```json
{
  "nodes": [],
  "connections": {}
}
```

## Response Shape

The endpoint always returns the same high-level fields used locally:

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
    "Expression format error in node ..."
  ],
  "feedback_block": "- Required property 'Name' cannot be empty\n- Expression format error in node ...",
  "repair_prompt": "Revise the n8n workflow JSON so it passes validator checks....",
  "errors": [],
  "warnings": [],
  "statistics": {},
  "suggestions": []
}
```

Notes:

- `repair_prompt` is included only when `original_prompt` is provided and `include_repair_prompt` is `true`.
- `valid=false` still returns `200 OK`.
- `extract_error="no_json_found"` is used when response text contains no extractable workflow object.
- `workflow` is only echoed when `debug=true` is sent.

## Expected Client Flow

Recommended plugin behavior:

1. Ask the model for workflow output as usual.
2. Call `POST /public/validate-workflow` with either:
   - `response_text` when validating the full assistant reply, or
   - `workflow` when the plugin already has parsed JSON.
3. If `valid` is `true`, continue normally.
4. If `valid` is `false`, use:
   - `feedback_block` for user-facing validation feedback, or
   - `repair_prompt` for an automatic retry attempt.
5. Keep retry orchestration client-side for now.

## Suggested Plugin Request Logic

- Prefer sending `workflow` when the plugin already extracted a JSON object.
- Otherwise send the raw `response_text` and let the server do extraction.
- Set `include_repair_prompt=true` only when the plugin intends to retry automatically.
- Pass the original user request as `original_prompt` when you want repair guidance that preserves the original task.

## Error Handling

- `400` malformed JSON or invalid request schema
- `413` request body too large
- `429` rate limited
- `503` validator unavailable or startup/runtime failure

## Example Curl

```bash
curl -sS -X POST "https://YOUR-HOST/public/validate-workflow" \
  -H "Content-Type: application/json" \
  -d '{
    "response_text": "```json\n{\"nodes\":[],\"connections\":{}}\n```",
    "original_prompt": "Build an n8n workflow that posts to Slack when a webhook fires",
    "max_errors": 8,
    "include_repair_prompt": true
  }'
```

## Implementation Notes In This Repo

- FastAPI endpoint: [ops-proxy/app.py](/Users/danielbennett/codeNew/n8n-hindsight/ops-proxy/app.py:142)
- Validation helpers and repair prompt logic: [ops-proxy/workflow_validator.py](/Users/danielbennett/codeNew/n8n-hindsight/ops-proxy/workflow_validator.py:162)
- Persistent Node bridge into `n8n-mcp`: [ops-proxy/validator_bridge.js](/Users/danielbennett/codeNew/n8n-hindsight/ops-proxy/validator_bridge.js:1)
- Docker install of `n8n-mcp`: [Dockerfile](/Users/danielbennett/codeNew/n8n-hindsight/Dockerfile:7), [ops-proxy/package.json](/Users/danielbennett/codeNew/n8n-hindsight/ops-proxy/package.json:1)

## Test Coverage Added Here

Run:

```bash
cd /Users/danielbennett/codeNew/n8n-hindsight/ops-proxy
pytest -q
```

Covered cases:

- valid raw workflow JSON
- invalid workflow with deduped repair messages
- prose-only response with `no_json_found`
- fenced JSON extraction
- bare embedded JSON extraction
- repair prompt generation
- request body size rejection
- malformed JSON request rejection
