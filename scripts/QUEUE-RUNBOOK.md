# Consolidation ("combination") queue runbook

The n8n Hindsight instance turns raw memories (docs, GitHub issues/PRs, community
posts, release notes) into consolidated **observations** via a background
**consolidation** worker. If consolidation stalls, recall quality degrades and the
backlog (`pending_consolidation`) stops dropping. This is the "combination queue".

## How consolidation runs

- The consolidation worker is **embedded in the `hindsight-api` process** (started
  in the app lifespan when `HINDSIGHT_API_WORKER_ENABLED` is true). It is *not* a
  separate `hindsight-worker` process in this deployment.
- It claims work as async operations in the `async_operations` table, marking each
  `pending -> processing -> completed/failed`.
- `worker_id = HINDSIGHT_API_WORKER_ID or socket.gethostname()`.

## Why the queue gets stuck

On startup the worker runs `recover_own_tasks()`, which resets tasks stuck in
`processing` **only where `worker_id` matches its own id**. There is **no
timeout-based reclaim of tasks owned by a different worker_id.**

So if the worker_id changes between restarts, any task the previous worker left in
`processing` is **orphaned** — no future worker will ever reclaim it, and it blocks
those memories permanently. This is exactly what happened before
`HINDSIGHT_API_WORKER_ID` was pinned to `n8nhindsight-worker-1`: each Appliku
redeploy gave the container a new random hostname, so every redeploy leaked
orphaned `processing` tasks.

Pinning the worker_id (commit adding `HINDSIGHT_API_WORKER_ID`) stops *new* leaks,
but does **not** clean up tasks already orphaned under old hostnames. Those must be
released manually — the public API can't do it (`cancel` only acts on `pending`,
`retry` only on `failed`/`cancelled`).

## Recover (run on the deployment)

Run these where `HINDSIGHT_API_DATABASE_URL`, `HINDSIGHT_API_TENANT_API_KEY` and
`hindsight-admin` are available — e.g. an Appliku one-off command or `exec` into the
web container. (The scripts are stdlib-only and shell out to the bundled
`hindsight-admin` CLI.)

1. **Diagnose:**
   ```bash
   python3 scripts/queue-status.py
   ```
   Shows the consolidation backlog, async-operation status, and `processing` tasks
   grouped by worker. Any worker_id other than `n8nhindsight-worker-1` is a dead
   worker holding orphaned tasks.

2. **Unstick (dry run first):**
   ```bash
   python3 scripts/unstick-queue.py          # preview
   python3 scripts/unstick-queue.py --yes    # execute
   ```
   This releases orphaned `processing` tasks (every worker_id except the live one)
   back to `pending`, recovers permanently-failed consolidation memories, and
   triggers a fresh consolidation pass. The live worker then drains the backlog — no
   redeploy required.

3. **Re-check:**
   ```bash
   python3 scripts/queue-status.py
   ```
   `pending_consolidation` should start falling and `last_consolidated_at` should
   become recent.

### If the LIVE worker itself is wedged

If `processing` tasks remain under `n8nhindsight-worker-1` and are not advancing
(check the `[WORKER_STATS]` / `[WORKER_TASK]` lines in the web logs — a hung LLM
call shows as a task whose `last_update` keeps growing), restart/redeploy the **web**
service on Appliku. The worker's `recover_own_tasks()` will safely release its own
in-flight tasks on startup. After the restart you can also run:
```bash
python3 scripts/unstick-queue.py --yes --all
```

## Equivalent manual commands

The scripts are thin wrappers; you can run the underlying tools directly:

```bash
hindsight-admin worker-status --schema public            # list processing tasks by worker
hindsight-admin decommission-worker <dead-worker-id> -y  # release one dead worker's tasks
hindsight-admin decommission-workers -y                  # release ALL processing tasks

# API (auth: Bearer $HINDSIGHT_API_TENANT_API_KEY)
curl -X POST $HINDSIGHT_URL/v1/default/banks/n8n/consolidation/recover -H "Authorization: Bearer $KEY"
curl -X POST $HINDSIGHT_URL/v1/default/banks/n8n/consolidate           -H "Authorization: Bearer $KEY"
curl       $HINDSIGHT_URL/v1/default/banks/n8n/stats                   -H "Authorization: Bearer $KEY"
```

## Prevention

- Keep `HINDSIGHT_API_WORKER_ID` pinned (currently `n8nhindsight-worker-1`). Never
  let it fall back to the container hostname, or redeploys will leak orphans again.
- If you ever *change* the pinned worker_id, decommission the old id first
  (`hindsight-admin decommission-worker <old-id> -y`) so its in-flight tasks aren't
  orphaned.
