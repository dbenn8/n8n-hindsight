# n8n-validator — workflow-validation microservice

A stateless microservice that wraps [n8n-mcp](https://github.com/czlonkowski/n8n-mcp)'s
workflow-validation engine and exposes it over HTTP. It is the validation backend for the
[n8n-knowledge](https://github.com/dbenn8/n8n-knowledge) plugin and the
[n8n-hindsight](https://github.com/dbenn8/n8n-hindsight) main service.

Deployed on Appliku as a separate app at `n8nvalidator.applikuapp.com`. The main n8n-hindsight
service forwards `POST /public/validate-workflow` here whenever
`WORKFLOW_VALIDATOR_FORWARD_URL` is set.

## What it wraps

n8n-mcp ships a full `WorkflowValidator` engine plus a `nodes.db` SQLite database describing
every n8n node (1,851 nodes), their operations, fields, types, and defaults. This service runs
that engine over a posted workflow JSON and returns structured errors and warnings (invalid
operation enums, missing required fields, expression-format errors, type mismatches, broken
connections). It holds no state between requests.

## Why it's a separate app

The validator is split out from the main service for two concrete reasons:

1. **Heavy, version-pinned native dependency.** n8n-mcp pulls in `better-sqlite3`, a native
   module compiled from source by `node-gyp` at install time (needs `python3`, `make`, `g++`).
   Keeping that build — and the exact pinned `n8n-mcp` version that determines which `nodes.db`
   you validate against — out of the main API image keeps the main image lean and lets the
   validator's version move independently.
2. **Independent scaling.** Validation is bursty and CPU-bound (it loads and queries the node
   database per request); recall is the steady-state hot path. Separating them means a flood of
   validations can't starve recall, and either can be scaled on its own.

## Health metadata contract

`GET /public/validator-health` (and the internal `/health`) returns the engine identity so a
client can verify it's validating against exactly the node data it expects **before** trusting a
result:

```json
{
  "validator_engine": "n8n-mcp",
  "configured_n8n_mcp_version": "...",
  "installed_n8n_mcp_version": "...",
  "nodes_db_sha256": "...",
  "nodes_content_sha256": "..."
}
```

- `configured_` vs `installed_` n8n-mcp version catches a drift between what the package
  declares and what actually got installed.
- `nodes_db_sha256` is a whole-file hash of `nodes.db`.
- `nodes_content_sha256` is a **logical** hash of the node table's content (see below) — this
  is the field clients compare for equivalence.

The plugin's eval preflight (`scripts/eval/validator_preflight.py` in n8n-knowledge) compares
this descriptor against its own local validator's descriptor and **fails closed on mismatch**,
so plugin-time validation and post-hoc scoring can never silently disagree about node data.

## The fail-closed preflight story (two hashing bugs)

Getting `nodes_content_sha256` to be a *stable, cross-environment* fingerprint took two
iterations, both real bugs:

1. **Whole-file hashing was unstable.** Hashing the `nodes.db` file directly produced false
   mismatches: n8n-mcp mutates its own SQLite at runtime (change counter, freed pages, FTS
   internals) without the node *data* changing — so a fresh install and a used one hashed
   differently on identical content. Fix: hash the **ordered rows of the nodes table**
   (`SELECT * FROM nodes ORDER BY node_type`) instead of the file bytes — i.e. hash the data
   that actually drives validation.

2. **`repr()`-based row hashing was interpreter-dependent.** The row hash first serialized
   values via `repr()`. That turned out to be Unicode-version dependent: Python 3.10 and 3.11
   escape certain code points differently, so an emoji in a node README (e.g. in a description
   field) hashed differently on 3.10 vs 3.11 over *identical* data. Fix: replace `repr()` with
   **explicit type-tagged byte serialization** — each value is prefixed with a one-byte type tag
   (`N` null, `O` bool, `I` int, `F` float, `B` bytes, `S` string) and encoded deterministically,
   with a row separator. Same data → same bytes → same hash, on any interpreter.

The two implementations are kept **byte-identical** by contract:
`ops-proxy/workflow_validator.py:_nodes_content_sha256` (this service) and
`hooks/lib/validator_metadata.py:_nodes_content_sha256` (the plugin) must produce the same
digest for the same `nodes.db`, and each file's docstring points at the other. That equivalence
is exactly what the fail-closed preflight checks.

## Build-context quirk

**This image must be built from the repo root**, not from inside `validator-app/`:

```
build context: <repo root of n8n-hindsight>
dockerfile_path: validator-app/Dockerfile
```

The Dockerfile's `COPY` instructions reference paths under `ops-proxy/` (e.g.
`COPY ops-proxy/package.json ...`, `COPY ops-proxy/ /app/`) because the validator reuses the
ops-proxy FastAPI app and its dependencies. Cloning or building `validator-app/` alone **will
fail** — those paths don't exist relative to that directory. Appliku is configured to build from
the repo root with `dockerfile_path` pointed here.

The image installs the native build toolchain (`python3`, `make`, `g++`) before
`npm install --omit=dev` so `better-sqlite3` compiles deterministically, then serves the FastAPI
app via uvicorn on port 8000.

## Run locally

```bash
# from the repo root:
docker build -f validator-app/Dockerfile -t n8n-validator .
docker run -p 8000:8000 n8n-validator
curl http://localhost:8000/public/validator-health
```

## Related

- [n8n-knowledge](https://github.com/dbenn8/n8n-knowledge) — the plugin; its eval preflight consumes the health contract above.
- [n8n-hindsight](https://github.com/dbenn8/n8n-hindsight) — the main knowledge service that forwards validation requests here.
