"""Shared helpers for the n8n -> Hindsight sync scripts.

This module factors out the logic that was previously copy-pasted across
sync-releases.py, sync-docs.py, sync-community.py, sync-code.py,
sync-github.py, sync-workflows.py and sync-nodes.py:

  - ``retain_batch`` — POST a batch of memory items to the Hindsight API
  - ``load_state`` / ``save_state`` — read/write the per-script JSON state file
  - ``resolve_env`` — resolve HINDSIGHT_URL / HINDSIGHT_KEY from the environment
  - ``build_arg_parser`` — argparse parser carrying the shared CLI flags

Behavior is intentionally identical to the original inline copies so the
extraction is provably non-functional-changing. See scripts/tests/.
"""
import argparse
import json
import os
import sys
import urllib.request

# --- Shared constants (previously magic numbers / inline literals) ---
BANK_ID = "n8n"
DEFAULT_HINDSIGHT_URL = "http://127.0.0.1:8889"
HINDSIGHT_KEY_ENV = "HINDSIGHT_API_TENANT_API_KEY"

BATCH_SIZE = 5          # items per retain request
RETAIN_SLEEP = 0.5      # pagination courtesy sleep (docs/community commit walks)
HTTP_TIMEOUT = 120      # seconds for the retain POST

# HTTP statuses the retain endpoint returns on success.
RETAIN_OK_STATUSES = (200, 201, 202)


def resolve_env():
    """Resolve (HINDSIGHT_URL, HINDSIGHT_KEY) from the environment.

    Matches the original inline blocks: HINDSIGHT_URL defaults to the
    in-container loopback address, the key comes from
    HINDSIGHT_API_TENANT_API_KEY and defaults to an empty string when unset.
    """
    url = os.environ.get("HINDSIGHT_URL", DEFAULT_HINDSIGHT_URL)
    key = os.environ.get(HINDSIGHT_KEY_ENV, "")
    return url, key


def load_state(state_file):
    """Load the JSON state dict from ``state_file`` ('{}' if it doesn't exist)."""
    if os.path.exists(state_file):
        with open(state_file) as f:
            return json.load(f)
    return {}


def save_state(state_file, state):
    """Write ``state`` to ``state_file`` (indent=2), creating parent dirs.

    ``os.path.dirname(...) or "."`` guards the case where ``state_file`` is a
    bare filename with no directory component (preserves sync-nodes behavior;
    harmless for the other scripts whose paths always had a directory).
    """
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def retain_batch(items, hindsight_url, hindsight_key, bank_id=BANK_ID, flush=True):
    """POST a batch of items to the Hindsight memories endpoint.

    Returns True on a 200/201/202 response, False on any non-success status or
    on any exception (logging "RETAIN ERROR: ..." to stderr and continuing —
    callers count the batch as failed). ``flush`` controls whether the stderr
    log is flushed: the original sync-github.py copy did NOT flush, every other
    copy did, so callers can opt out to keep byte-for-byte parity.
    """
    payload = json.dumps({"items": items, "async": True}).encode()
    headers = {
        "Authorization": f"Bearer {hindsight_key}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        f"{hindsight_url}/v1/default/banks/{bank_id}/memories",
        data=payload, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status in RETAIN_OK_STATUSES
    except Exception as e:
        print(f"  RETAIN ERROR: {e}", file=sys.stderr, flush=flush)
        return False


def build_arg_parser(description=None, *, full=True, dry_run=True, test=True):
    """Build an argparse parser carrying the shared sync CLI flags.

    Flag semantics match the original hand-rolled sys.argv parsing exactly:
      --full      store_true
      --dry-run   store_true
      --test N    optional int; bare ``--test`` (no value) defaults to 5

    Scripts add their own extra flags onto the returned parser. ``parse_known``
    is the recommended entry point for callers that still need to inspect
    raw argv, but ``parser.parse_args()`` works for the common case.
    """
    # allow_abbrev=False so e.g. "--dr" does NOT silently match "--dry-run";
    # the original hand-rolled `"--dry-run" in sys.argv` checks required the
    # exact flag spelling.
    parser = argparse.ArgumentParser(description=description, allow_abbrev=False)
    if full:
        parser.add_argument("--full", action="store_true")
    if dry_run:
        parser.add_argument("--dry-run", action="store_true")
    if test:
        # nargs="?" with const=5 reproduces "bare --test means 5".
        parser.add_argument("--test", type=int, nargs="?", const=5, default=None)
    return parser
