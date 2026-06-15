"""Tests for GitHub issue/PR node:X tagging in sync-github.py.

Covers the engagement-gated node-detection added to format_item, plus a
cross-repo PARITY GUARD on the vendored node_lookup.py.
"""
import hashlib
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)

# Pinned canonical sha256 of the vendored scripts/lib/node_lookup.py. The
# IDENTICAL literal is pinned in n8n-knowledge:
#   tests/test-node-lookup-parity.sh
# node_lookup.py is the SINGLE canonical node-detection logic; the plugin must
# vendor it (it ships to users) and this repo keeps a byte-identical copy for the
# ingest side, so the node:X tags written here match what the plugin's
# do_gotcha_recall queries. If either copy drifts, that repo's suite fails here
# (same pattern as the validator hash-parity guard). To change the detector:
# edit the canonical file in n8n-knowledge, re-vendor to scripts/lib/, recompute
# the hash (`shasum -a 256 scripts/lib/node_lookup.py`), and update BOTH pinned
# literals. A red test means a copy drifted — never weaken the test.
PINNED_NODE_LOOKUP_SHA256 = (
    "bc8ea6c573a1b0bc2f534145942dca722d9b819ede3ffefe4b8671b341842ae8"
)


def _load_sync_github():
    os.environ.setdefault("HINDSIGHT_API_TENANT_API_KEY", "dummy")
    os.environ.setdefault("HINDSIGHT_URL", "http://127.0.0.1:9")
    spec = importlib.util.spec_from_file_location(
        "sync_github_mod", os.path.join(SCRIPTS, "sync-github.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _issue(title, body="", reactions=0, comments=0, number=1, state="open"):
    return {
        "number": number, "title": title, "body": body,
        "html_url": f"https://github.com/n8n-io/n8n/issues/{number}",
        "labels": [], "created_at": "2026-01-01",
        "reactions": {"total_count": reactions}, "comments": comments,
        "state": state, "author_association": "NONE",
    }


def _node_tags(item):
    m = _load_sync_github()
    return [t for t in m.format_item(item, "issue")["tags"] if t.startswith("node:")]


def test_parity_node_lookup_hash():
    path = os.path.join(SCRIPTS, "lib", "node_lookup.py")
    with open(path, "rb") as f:
        actual = hashlib.sha256(f.read()).hexdigest()
    assert actual == PINNED_NODE_LOOKUP_SHA256, (
        "vendored scripts/lib/node_lookup.py drifted from the canonical copy in "
        "n8n-knowledge. Re-vendor the identical file and update both pinned "
        "literals. Do NOT weaken this test — a red result means a copy drifted."
    )


def test_high_engagement_real_node_is_tagged():
    assert _node_tags(_issue(
        "Supabase node rejects valid API credentials",
        "Auth failed on supabase node", reactions=3, comments=2,
    )) == ["node:supabase"]


def test_below_floor_not_tagged():
    assert _node_tags(_issue(
        "Supabase node rejects valid API credentials", "Auth failed",
        reactions=0, comments=0,
    )) == []


def test_generic_workflows_no_false_positive():
    # High engagement but no real node mentioned -> must NOT stamp
    # node:workflowTrigger (the plural-'workflows' false-positive fix).
    assert _node_tags(_issue(
        "Improve editor performance for large workflows", "editor is slow",
        reactions=10, comments=5,
    )) == []


def test_engagement_floor_boundary_inclusive():
    # 1 reaction + 1 comment * 4 = 5 == RETAIN_ENGAGEMENT_FLOOR -> tagged.
    assert _node_tags(_issue(
        "Wait node never resumes after delay", "hangs",
        reactions=1, comments=1,
    )) == ["node:wait"]


def test_floor_constant_is_five():
    assert _load_sync_github().RETAIN_ENGAGEMENT_FLOOR == 5
