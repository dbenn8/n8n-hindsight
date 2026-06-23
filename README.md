# n8n-hindsight — knowledge service + validator behind the n8n-knowledge plugin

This repo is the backend for the [n8n-knowledge](https://github.com/dbenn8/n8n-knowledge)
Claude Code plugin. It does two things:

1. **Knowledge service** — a sync pipeline that ingests n8n's docs, GitHub, community forum,
   releases, source code, node specs, and workflow examples into a
   [Hindsight](https://hindsight.vectorize.io) graph-memory bank (bank id `n8n`, **436k+
   memories**), fronted by an **ops-proxy** (FastAPI) and **nginx** that expose hardened,
   rate-limited public endpoints.
2. **Workflow validator** — a stateless microservice (see
   [`validator-app/`](validator-app/README.md)) that wraps n8n-mcp's validation engine. The
   main app forwards `POST /public/validate-workflow` to it and exposes a health/version
   contract so clients can fail closed on validator mismatch.

Deployed on [Appliku](https://appliku.com) as **two apps** (main service + validator), both
from Dockerfiles in this repo.

## Architecture

```mermaid
flowchart TD
    subgraph sync["Sync pipeline (cron + on-release)"]
        D["sync-docs.py"]
        G["sync-github.py"]
        C["sync-community.py"]
        R["sync-releases.py"]
        CODE["sync-code.py"]
        N["sync-nodes.py"]
        W["sync-workflows.py"]
    end

    subgraph app["Main Appliku app (supervisord)"]
        NGINX["nginx<br/>rate limits + key injection"]
        OPS["ops-proxy (FastAPI)<br/>validate-workflow forward, /logs"]
        API["Hindsight API<br/>local embeddings, pgvector"]
        BANK[("Hindsight bank: n8n<br/>436k+ memories")]
    end

    VAL["validator-app (separate Appliku app)<br/>n8nvalidator.applikuapp.com<br/>wraps n8n-mcp engine"]

    DB[("PostgreSQL 16 + pgvector")]
    LLM["LLM gateway<br/>(currently DeepSeek)<br/>consolidation only"]

    D & G & C & R & CODE & N & W -->|retain| API
    API --> BANK
    API --> DB
    API -. consolidation .-> LLM

    CLIENT["plugin / curl"] --> NGINX
    NGINX -->|/public/recall, /public/stats| API
    NGINX -->|/public/validate-workflow| OPS
    NGINX -->|/public/validator-health| OPS
    NGINX -->|/logs (key-gated)| OPS
    OPS -->|WORKFLOW_VALIDATOR_FORWARD_URL| VAL
```

### Sync pipeline

Seven sync scripts under [`scripts/`](scripts/), each ingesting one source into the `n8n` bank:

| Script | Ingests |
|---|---|
| `sync-docs.py` | Official documentation (docs.n8n.io), incremental on changed files |
| `sync-github.py` | GitHub issues & PRs with canonical state (open/closed·completed/closed·not_planned) |
| `sync-community.py` | community.n8n.io forum questions, answers, solved status, engagement |
| `sync-releases.py` | n8n release notes / changelog entries, incremental on new releases |
| `sync-code.py` | n8n source code across core packages, incremental on changed files |
| `sync-nodes.py` | node specs from n8n-mcp's `nodes.db`, splitting big multi-resource nodes into per-resource/per-operation units |
| `sync-workflows.py` | official workflow examples (topology + importable JSON) |

Four run nightly as Appliku cronjobs (UTC):

| Cron | Script | Schedule |
|---|---|---|
| `github-sync` | `sync-github.py` | `0 3 * * *` (03:00) |
| `docs-sync` | `sync-docs.py` | `30 3 * * *` (03:30) |
| `community-sync` | `sync-community.py` | `0 4 * * *` (04:00) |
| `releases-sync` | `sync-releases.py` | `30 4 * * *` (04:30) |

`sync-nodes.py`, `sync-workflows.py`, and `sync-code.py` are run on-release / on-demand rather
than nightly (node and workflow corpora only change when n8n ships, and a full code re-sync is
expensive). `find-duplicates.py` / `delete-duplicates.py` are maintenance utilities, not part
of the ingest path. Sync cursor state lives in `/data/sync-state.json` on a persistent volume.

### Public endpoints

nginx terminates all public traffic, applies per-IP rate limits, and injects the upstream
Hindsight key server-side so the public endpoints stay unauthenticated but key-free:

| Endpoint | Auth | Backed by | Notes |
|---|---|---|---|
| `POST /public/recall` | none, rate-limited | Hindsight `n8n` bank recall | 20 req/min/IP, burst 10 |
| `GET /public/stats` | none, rate-limited | Hindsight `n8n` bank stats | 20 req/min/IP |
| `POST /public/validate-workflow` | none, rate-limited | ops-proxy → validator | 30 req/min, body-size capped, forwarded to validator app |
| `GET /public/validator-health` | none | ops-proxy | engine versions + `nodes_db_sha256` + `nodes_content_sha256` |
| `GET /logs` | **admin key** | ops-proxy | key-gated, constant-time compare, rate-limited (see below) |

Internal surfaces (`/metrics`, `/docs`, `/openapi.json`) are returned `404` publicly.

### Hardened `/logs`

The durable-log retrieval endpoint (`GET /logs` in `ops-proxy/app.py`) is the one
authenticated public route. It:

- requires a `Bearer` token compared against `LOGS_ADMIN_KEY` with `hmac.compare_digest`
  (**constant-time**, no early-exit timing oracle);
- is rate-limited (30/min);
- validates the `service` name against an allow-list regex, caps `grep` pattern length and
  line counts, and rejects malformed regex — so it can't be turned into a file-read or ReDoS primitive.

### Validator forward-mode

When `WORKFLOW_VALIDATOR_FORWARD_URL` is set, `ops-proxy` forwards `POST
/public/validate-workflow` to the dedicated validator app instead of validating in-process.
This decouples the validator's heavy native dependency (n8n-mcp → better-sqlite3, compiled from
source) and its version pinning from the main service. See
[`validator-app/README.md`](validator-app/README.md) for the full story including the
fail-closed health contract.

### Consolidation

Hindsight consolidates retained memories asynchronously via an **LLM gateway (currently
DeepSeek)**. Consolidation runs in parallel with retain and is the only LLM cost in the
pipeline — recall itself is zero-LLM-cost. Embeddings are computed by a **local in-container
model** (no external embedding API); the reranker is RRF; vectors live in pgvector.

## Deploy on Appliku

Two apps, both built from Dockerfiles in this repo:

1. **Main service** (`Dockerfile` + `appliku.yml`) — supervisord runs the Hindsight API,
   nginx, ops-proxy, and the log writer. Appliku provisions PostgreSQL 16 + pgvector
   automatically. Set these as dashboard secrets (the repo is public):
   - `HINDSIGHT_API_LLM_API_KEY` — key for the consolidation LLM gateway
   - `HINDSIGHT_API_TENANT_API_KEY` — internal tenant key (injected server-side by nginx)
   - `LOGS_ADMIN_KEY` — admin key for `GET /logs`
   - `GITHUB_TOKEN` — for the GitHub sync cron
   - `WORKFLOW_VALIDATOR_FORWARD_URL` — URL of the validator app (enables forward-mode)
2. **Validator app** (`validator-app/Dockerfile`) — built **from the repo root** with
   `dockerfile_path=validator-app/Dockerfile`. See
   [`validator-app/README.md`](validator-app/README.md) for the build-context quirk.

## Query the API

```bash
curl -X POST https://n8nhindsight.applikuapp.com/public/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "how to install n8n with docker", "budget": "mid"}'
```

The public endpoint targets the `n8n` bank and needs no key (rate-limited). The raw Hindsight
API (`/v1/default/banks/n8n/memories/recall`) requires the tenant key and is not exposed publicly.

## Run / test

```bash
python3 -m pytest ops-proxy/tests/ logwriter/tests/
```

Covers the hardened `/logs` endpoint, workflow-validation forwarding, and the log writer
(currently **27 passing**).

## Related

- [n8n-knowledge](https://github.com/dbenn8/n8n-knowledge) — the Claude Code plugin that consumes this service.
- [`validator-app/`](validator-app/README.md) — the workflow-validator microservice.
