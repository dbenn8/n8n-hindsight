#!/usr/bin/env python3
"""
Diagnose the n8n Hindsight consolidation ("combination") queue.

Read-only. Reports the consolidation backlog and async-operation status from the
API, then (if HINDSIGHT_API_DATABASE_URL is set and the bundled `hindsight-admin`
CLI is available) lists in-flight `processing` tasks grouped by worker so you can
spot orphaned tasks left behind by a dead worker_id.

Why orphaned tasks happen: the consolidation worker is embedded in hindsight-api
and only reclaims stuck `processing` tasks matching its OWN worker_id on startup
(recover_own_tasks). There is no timeout-based reclaim of other workers' tasks.
If the worker_id ever changes (e.g. an ephemeral container hostname before
HINDSIGHT_API_WORKER_ID was pinned), tasks left in `processing` by the old id are
never reclaimed and silently block consolidation. See QUEUE-RUNBOOK.md.

Usage:
    python3 queue-status.py
"""
import json
import os
import shutil
import subprocess
import urllib.request

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
BANK_ID = "n8n"
WORKER_ID = os.environ.get("HINDSIGHT_API_WORKER_ID", "")
SCHEMA = os.environ.get("HINDSIGHT_DB_SCHEMA", "public")


def api_get(path):
    url = f"{HINDSIGHT_URL}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {HINDSIGHT_KEY}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    print("=== Consolidation queue status ===\n")

    try:
        stats = api_get(f"/v1/default/banks/{BANK_ID}/stats")
    except Exception as e:
        print(f"ERROR: could not fetch /stats from {HINDSIGHT_URL}: {e}")
        print("Is HINDSIGHT_URL / HINDSIGHT_API_TENANT_API_KEY set correctly?")
        return 1

    by_status = stats.get("operations_by_status", {}) or {}
    pending_c = stats.get("pending_consolidation", 0)
    failed_c = stats.get("failed_consolidation", 0)
    last_c = stats.get("last_consolidated_at")

    print(f"Documents:               {stats.get('total_documents', 0)}")
    print(f"Observations:            {stats.get('total_observations', 0)}")
    print(f"Pending consolidation:   {pending_c}   (memories not yet turned into observations)")
    print(f"Failed consolidation:    {failed_c}   (permanently failed — recover with unstick-queue.py)")
    print(f"Last consolidated at:    {last_c or 'never'}")
    print()
    print("Async operations by status:")
    for status in ("pending", "processing", "completed", "failed", "cancelled"):
        if status in by_status:
            print(f"  {status:<12s} {by_status[status]}")
    for status, count in by_status.items():
        if status not in ("pending", "processing", "completed", "failed", "cancelled"):
            print(f"  {status:<12s} {count}")
    print()

    # Worker / orphaned-task view (needs DB access + hindsight-admin)
    admin = shutil.which("hindsight-admin")
    if not os.environ.get("HINDSIGHT_API_DATABASE_URL"):
        print("(Skipping worker view: HINDSIGHT_API_DATABASE_URL not set in this environment.)")
    elif not admin:
        print("(Skipping worker view: `hindsight-admin` not on PATH in this container.)")
    else:
        print(f"Configured worker_id (HINDSIGHT_API_WORKER_ID): {WORKER_ID or '(unset — falls back to hostname!)'}")
        print("In-flight `processing` tasks by worker (`hindsight-admin worker-status`):\n")
        try:
            out = subprocess.run(
                [admin, "worker-status", "--schema", SCHEMA],
                capture_output=True, text=True, timeout=60,
            )
            print(out.stdout.rstrip() or "(no output)")
            if out.stderr.strip():
                print(out.stderr.rstrip())
        except Exception as e:
            print(f"  worker-status failed: {e}")
        print()
        print("Tasks listed under any worker_id OTHER than the configured one above are")
        print("orphaned (their worker no longer exists) and block the queue. Run:")
        print("  python3 scripts/unstick-queue.py        # dry run")
        print("  python3 scripts/unstick-queue.py --yes  # release them + retrigger")

    # Quick verdict
    print("\n=== Verdict ===")
    processing = by_status.get("processing", 0)
    if processing and pending_c:
        print(f"{processing} task(s) in `processing` while {pending_c} memories await consolidation.")
        print("If those tasks are old / under a stale worker_id, the queue is STUCK on orphans.")
    elif failed_c:
        print(f"{failed_c} memories permanently failed consolidation — run unstick-queue.py to recover them.")
    elif pending_c:
        print(f"{pending_c} memories pending; no obvious orphan/failure — queue may just be working through backlog.")
    else:
        print("No pending consolidation — queue is caught up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
