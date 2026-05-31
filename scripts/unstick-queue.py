#!/usr/bin/env python3
"""
Unstick the n8n Hindsight consolidation ("combination") queue.

What "stuck" means here: consolidation runs as async operations processed by a
worker embedded in hindsight-api. The worker only reclaims `processing` tasks
that match its OWN worker_id on startup (recover_own_tasks); there is no
timeout-based reclaim of tasks left behind by a different/dead worker_id. So
tasks left in `processing` by an old container (e.g. before HINDSIGHT_API_WORKER_ID
was pinned, when worker_id fell back to the random container hostname) are
orphaned forever and block the queue. The public API can't clear them
(cancel only touches `pending`, retry only `failed`/`cancelled`).

This script performs a safe recovery:
  1. Release orphaned `processing` tasks back to `pending` so the LIVE worker
     drains them. Uses the bundled `hindsight-admin decommission-worker` CLI
     (DB-level reset). By default it only decommissions worker_ids OTHER than the
     currently-configured HINDSIGHT_API_WORKER_ID, so it never disturbs the live
     worker's genuinely in-flight tasks. Use --all to release every `processing`
     task (do this only after the live worker is restarted/redeployed).
  2. Recover memories that permanently FAILED consolidation (POST /consolidation/recover).
  3. Trigger a fresh consolidation pass (POST /consolidate).

Run it on the box / via an Appliku one-off (it needs HINDSIGHT_API_DATABASE_URL
for step 1 and the API for steps 2-3).

Usage:
    python3 unstick-queue.py              # dry run — show what it would do
    python3 unstick-queue.py --yes        # execute
    python3 unstick-queue.py --yes --all  # also release the live worker's tasks
    python3 unstick-queue.py --yes --no-recover --no-trigger   # only release orphans

See QUEUE-RUNBOOK.md for the full diagnosis/recovery runbook.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import urllib.request

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
BANK_ID = "n8n"
WORKER_ID = os.environ.get("HINDSIGHT_API_WORKER_ID", "")
SCHEMA = os.environ.get("HINDSIGHT_DB_SCHEMA", "public")


def api_call(method, path, data=None):
    url = f"{HINDSIGHT_URL}{path}"
    headers = {"Authorization": f"Bearer {HINDSIGHT_KEY}"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    req = urllib.request.Request(url, headers=headers, method=method, data=body)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def list_processing_workers(admin):
    """Return {worker_id: task_count} for tasks currently in `processing`."""
    out = subprocess.run(
        [admin, "worker-status", "--schema", SCHEMA],
        capture_output=True, text=True, timeout=60,
    )
    workers = {}
    for line in out.stdout.splitlines():
        m = re.match(r"^Worker:\s+(\S+)\s+\((\d+)\s+task", line)
        if m:
            workers[m.group(1)] = int(m.group(2))
    return workers, out.stdout


def main():
    ap = argparse.ArgumentParser(description="Unstick the Hindsight consolidation queue")
    ap.add_argument("--yes", "-y", action="store_true", help="Execute (default is dry-run)")
    ap.add_argument("--all", action="store_true",
                    help="Release ALL processing tasks, including the live worker's "
                         "(only safe after the worker is restarted/redeployed)")
    ap.add_argument("--no-recover", action="store_true", help="Skip /consolidation/recover")
    ap.add_argument("--no-trigger", action="store_true", help="Skip /consolidate trigger")
    ap.add_argument("--schema", default=SCHEMA, help="DB schema (default: public)")
    args = ap.parse_args()
    schema = args.schema
    dry = not args.yes

    print(f"{'=== DRY RUN ===' if dry else '=== EXECUTING ==='}")
    print(f"Configured live worker_id: {WORKER_ID or '(unset — falls back to hostname!)'}\n")

    # --- Step 1: release orphaned processing tasks --------------------------
    admin = shutil.which("hindsight-admin")
    if not os.environ.get("HINDSIGHT_API_DATABASE_URL"):
        print("STEP 1 SKIPPED: HINDSIGHT_API_DATABASE_URL not set — can't release orphaned tasks.")
        print("  Run this on the deployment (Appliku one-off / exec) where the DB URL is present.")
    elif not admin:
        print("STEP 1 SKIPPED: `hindsight-admin` not on PATH in this container.")
    else:
        workers, raw = list_processing_workers(admin)
        print("STEP 1: release orphaned `processing` tasks")
        if not workers:
            print("  No tasks in `processing` — nothing to release.")
        else:
            print("  Current processing tasks by worker:")
            for wid, cnt in workers.items():
                tag = "  <-- LIVE worker" if wid == WORKER_ID else "  <-- ORPHAN (dead worker)"
                print(f"    {wid}: {cnt}{tag if WORKER_ID else ''}")
            if args.all:
                targets = list(workers.keys())
            else:
                targets = [w for w in workers if w != WORKER_ID] if WORKER_ID else list(workers.keys())
                if not WORKER_ID:
                    print("  WARNING: HINDSIGHT_API_WORKER_ID is unset, so the live worker can't be "
                          "distinguished. Releasing ALL processing tasks.")
            if not targets:
                print("  No orphaned workers to release (only the live worker is processing).")
            for wid in targets:
                if dry:
                    print(f"  [dry-run] would: hindsight-admin decommission-worker {wid} --schema {schema} --yes")
                else:
                    r = subprocess.run(
                        [admin, "decommission-worker", wid, "--schema", schema, "--yes"],
                        capture_output=True, text=True, timeout=120,
                    )
                    print(f"  {r.stdout.strip()}" + (f"\n  {r.stderr.strip()}" if r.stderr.strip() else ""))
    print()

    # --- Step 2: recover permanently-failed consolidation -------------------
    if args.no_recover:
        print("STEP 2 SKIPPED (--no-recover)")
    else:
        print("STEP 2: recover permanently-failed consolidation memories")
        if dry:
            print(f"  [dry-run] would: POST /v1/default/banks/{BANK_ID}/consolidation/recover")
        else:
            try:
                r = api_call("POST", f"/v1/default/banks/{BANK_ID}/consolidation/recover")
                print(f"  reset {r.get('retried_count', 0)} failed memories for retry")
            except Exception as e:
                print(f"  recover failed: {e}")
    print()

    # --- Step 3: trigger a fresh consolidation pass -------------------------
    if args.no_trigger:
        print("STEP 3 SKIPPED (--no-trigger)")
    else:
        print("STEP 3: trigger a consolidation pass")
        if dry:
            print(f"  [dry-run] would: POST /v1/default/banks/{BANK_ID}/consolidate")
        else:
            try:
                r = api_call("POST", f"/v1/default/banks/{BANK_ID}/consolidate")
                print(f"  consolidation queued: operation_id={r.get('operation_id')}")
            except Exception as e:
                print(f"  trigger failed: {e}")
    print()

    if dry:
        print("Dry run only. Re-run with --yes to execute.")
    else:
        print("Done. Re-run scripts/queue-status.py in a minute to confirm the queue is draining.")
        print("If `processing` tasks remain under the LIVE worker_id and are not advancing, the")
        print("live worker itself is wedged — restart/redeploy the web service on Appliku")
        print("(startup recover_own_tasks will release them safely), then re-run with --all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
