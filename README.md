# n8n-hindsight

Public Hindsight graph memory instance for n8n docs, issues, and related enablement content. Provides a queryable knowledge base via the Hindsight recall API.

## What's included

- Hindsight API with local embeddings (CPU-only, no external embedding API needed)
- PostgreSQL with pgvector for vector storage
- MCP server restricted to read-only tools (recall, list_banks, get_bank, get_bank_stats, list_tags)
- GreenPT (EU-based, privacy-focused) for LLM consolidation

## Deploy on Appliku

1. Create a new app on Appliku from this repo
2. Set the manual environment variables:
   - `HINDSIGHT_API_LLM_API_KEY` — your GreenPT API key
   - `HINDSIGHT_API_TENANT_API_KEY` — choose a new API key for this instance
3. Deploy — Appliku will provision the PostgreSQL database automatically

## Query the API

```bash
curl -X POST https://your-app.applikuapp.com/v1/default/banks/n8n-docs/memories/recall \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "how to install n8n with docker", "budget": "mid"}'
```

## Use with Claude Code

Install the n8n-docs plugin (coming soon) or add the Hindsight MCP server pointing at this instance.

## Content

### Phase 1: Official Documentation
315 pages from n8n's official documentation (docs.n8n.io), covering:
- Advanced AI (agents, evaluations, RAG, MCP, LangChain)
- Hosting (Docker, scaling, security, configuration, environment variables)
- Code (code node, expressions, built-in methods, cookbook)
- Data (structure, mapping, filtering, expressions reference)
- Flow logic (error handling, loops, merging, subworkflows)
- Courses (Level 1 and Level 2)
- API, credentials, user management, source control

### Future Phases
- GitHub issues (high-signal, filtered)
- GitHub discussions
- Community enablement content
