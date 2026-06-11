"""Characterization tests: pin the CURRENT behavior of the sync scripts.

These tests are written BEFORE the sync_common.py extraction. They lock down
the exact observable behavior of the shared logic (retain_batch request shape,
state load/save, env resolution, argv flag parsing) so the refactor can be
proven behavior-preserving. They must stay green across the extraction.

All HTTP is mocked — no network calls.
"""
import importlib
import io
import json
import os
import sys

import pytest

from _sync_test_helpers import load_script, SCRIPTS_DIR


# The seven scripts that define an identical-ish retain_batch / state pair.
RETAIN_SCRIPTS = [
    "sync-releases.py",
    "sync-docs.py",
    "sync-community.py",
    "sync-code.py",
    "sync-github.py",
    "sync-workflows.py",
    "sync-nodes.py",
]


# ---------------------------------------------------------------------------
# A fake urllib response usable as a context manager.
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ===========================================================================
# retain_batch — request shape, URL, headers, status handling, errors
# ===========================================================================

@pytest.mark.parametrize("script", RETAIN_SCRIPTS)
def test_retain_batch_request_shape(script, monkeypatch):
    """retain_batch posts {"items":..., "async":True} to the memories
    endpoint with bearer auth + JSON content-type, and treats 200/201/202 as
    success."""
    mod = load_script(script)
    monkeypatch.setattr(mod, "HINDSIGHT_URL", "http://example.test:9999")
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "secret-key")
    monkeypatch.setattr(mod, "BANK_ID", "n8n")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["data"] = req.data
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        return FakeResponse(status=202)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    items = [{"document_id": "a"}, {"document_id": "b"}]
    ok = mod.retain_batch(items)

    assert ok is True
    assert captured["url"] == "http://example.test:9999/v1/default/banks/n8n/memories"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 120

    payload = json.loads(captured["data"].decode())
    assert payload == {"items": items, "async": True}

    # Header names are title-cased by urllib's Request.add_header.
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["authorization"] == "Bearer secret-key"
    assert headers["content-type"] == "application/json"


@pytest.mark.parametrize("script", RETAIN_SCRIPTS)
@pytest.mark.parametrize("status,expected", [(200, True), (201, True), (202, True), (204, False), (400, False), (500, False)])
def test_retain_batch_status_codes(script, status, expected, monkeypatch):
    mod = load_script(script)
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "k")
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResponse(status=status))
    assert mod.retain_batch([{"x": 1}]) is expected


@pytest.mark.parametrize("script", RETAIN_SCRIPTS)
def test_retain_batch_exception_returns_false(script, monkeypatch, capsys):
    """On any exception retain_batch returns False and logs to stderr."""
    mod = load_script(script)
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "k")

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    assert mod.retain_batch([{"x": 1}]) is False
    err = capsys.readouterr().err
    assert "RETAIN ERROR" in err
    assert "connection refused" in err


# ===========================================================================
# load_state / save_state round-trip + missing-file behavior
# ===========================================================================

@pytest.mark.parametrize("script", RETAIN_SCRIPTS)
def test_load_state_missing_returns_empty_dict(script, monkeypatch, tmp_path):
    mod = load_script(script)
    monkeypatch.setattr(mod, "STATE_FILE", str(tmp_path / "nope.json"))
    assert mod.load_state() == {}


@pytest.mark.parametrize("script", RETAIN_SCRIPTS)
def test_save_then_load_state_roundtrip(script, monkeypatch, tmp_path):
    mod = load_script(script)
    state_file = tmp_path / "sub" / "state.json"
    monkeypatch.setattr(mod, "STATE_FILE", str(state_file))
    mod.save_state({"last_sync": "2024-01-01T00:00:00Z", "total_synced": 7})
    # makedirs created the parent dir.
    assert state_file.exists()
    assert mod.load_state() == {"last_sync": "2024-01-01T00:00:00Z", "total_synced": 7}
    # Pretty-printed with indent=2.
    text = state_file.read_text()
    assert "  " in text


def test_nodes_save_state_handles_bare_filename(monkeypatch, tmp_path):
    """sync-nodes uniquely guards os.path.dirname('') with `or '.'` so a bare
    filename (no directory) does not blow up makedirs."""
    mod = load_script("sync-nodes.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "STATE_FILE", "bare-state.json")
    mod.save_state({"k": "v"})
    assert (tmp_path / "bare-state.json").exists()


# ===========================================================================
# STATE_FILE / env-var resolution (read at import time)
# ===========================================================================

def _reimport_with_env(filename, env):
    """Import a script fresh with a controlled environment so the module-level
    os.environ.get(...) calls resolve against `env`."""
    for k in ["HINDSIGHT_URL", "HINDSIGHT_API_TENANT_API_KEY", "GITHUB_TOKEN",
              "STATE_FILE", "SYNC_STATE_FILE", "SYNC_RELEASES_STATE_FILE",
              "SYNC_DOCS_STATE_FILE", "SYNC_COMMUNITY_STATE_FILE",
              "SYNC_CODE_STATE_FILE", "SYNC_WORKFLOWS_STATE_FILE"]:
        os.environ.pop(k, None)
    os.environ.update(env)
    name = filename.replace("-", "_").replace(".py", "") + "_envtest"
    return load_script(filename, module_name=name)


def test_releases_default_state_file():
    mod = _reimport_with_env("sync-releases.py", {})
    assert mod.STATE_FILE == "/data/sync-releases-state.json"


def test_releases_state_file_env_override():
    mod = _reimport_with_env("sync-releases.py",
                             {"SYNC_RELEASES_STATE_FILE": "/x/r.json"})
    assert mod.STATE_FILE == "/x/r.json"


def test_github_uses_sync_state_file_env():
    mod = _reimport_with_env("sync-github.py", {})
    assert mod.STATE_FILE == "/data/sync-state.json"
    mod2 = _reimport_with_env("sync-github.py", {"SYNC_STATE_FILE": "/x/g.json"})
    assert mod2.STATE_FILE == "/x/g.json"


def test_nodes_uses_plain_state_file_env():
    mod = _reimport_with_env("sync-nodes.py", {})
    assert mod.STATE_FILE == "/data/sync-nodes-state.json"
    mod2 = _reimport_with_env("sync-nodes.py", {"STATE_FILE": "/x/n.json"})
    assert mod2.STATE_FILE == "/x/n.json"


def test_workflows_state_file_fallback_chain():
    """sync-workflows resolves SYNC_WORKFLOWS_STATE_FILE, then STATE_FILE,
    then the hardcoded default — in that order."""
    # 1. neither set -> default
    mod = _reimport_with_env("sync-workflows.py", {})
    assert mod.STATE_FILE == "/data/sync-workflows-state.json"
    # 2. only STATE_FILE set -> falls back to it
    mod = _reimport_with_env("sync-workflows.py", {"STATE_FILE": "/x/legacy.json"})
    assert mod.STATE_FILE == "/x/legacy.json"
    # 3. specific var wins over generic STATE_FILE
    mod = _reimport_with_env("sync-workflows.py",
                             {"SYNC_WORKFLOWS_STATE_FILE": "/x/wf.json",
                              "STATE_FILE": "/x/legacy.json"})
    assert mod.STATE_FILE == "/x/wf.json"


def test_hindsight_url_and_key_resolution():
    mod = _reimport_with_env("sync-releases.py", {})
    assert mod.HINDSIGHT_URL == "http://127.0.0.1:8889"
    assert mod.HINDSIGHT_KEY == ""
    mod2 = _reimport_with_env("sync-releases.py",
                              {"HINDSIGHT_URL": "https://h.example",
                               "HINDSIGHT_API_TENANT_API_KEY": "abc123"})
    assert mod2.HINDSIGHT_URL == "https://h.example"
    assert mod2.HINDSIGHT_KEY == "abc123"


# ===========================================================================
# Missing-key guard: scripts that require HINDSIGHT_KEY exit(1)
# ===========================================================================

def test_releases_main_exits_without_key(monkeypatch, capsys):
    mod = load_script("sync-releases.py")
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "")
    monkeypatch.setattr(sys, "argv", ["sync-releases.py"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert "HINDSIGHT_API_TENANT_API_KEY not set" in capsys.readouterr().err


# ===========================================================================
# argv flag parsing: --full / --dry-run / --test N
# ===========================================================================

def test_releases_dry_run_flag(monkeypatch, capsys):
    """--dry-run prints a DRY RUN summary and never retains."""
    mod = load_script("sync-releases.py")
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "k")
    monkeypatch.setattr(mod, "fetch_all_releases", lambda: [
        {"tag_name": "n8n@1.2.3", "published_at": "2024-01-01T00:00:00Z",
         "body": "x" * 50, "html_url": "http://r"},
    ])

    def fail_retain(items):
        raise AssertionError("retain_batch must not be called in --dry-run")

    monkeypatch.setattr(mod, "retain_batch", fail_retain)
    monkeypatch.setattr(sys, "argv", ["sync-releases.py", "--dry-run"])
    mod.main()
    out = capsys.readouterr().out
    assert "DRY RUN" in out


def test_releases_test_flag_limits_count(monkeypatch, capsys):
    """--test N limits processing to N releases."""
    mod = load_script("sync-releases.py")
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "k")
    releases = [
        {"tag_name": f"n8n@1.0.{i}", "published_at": "2024-01-01T00:00:00Z",
         "body": "x" * 50, "html_url": f"http://r{i}"}
        for i in range(10)
    ]
    monkeypatch.setattr(mod, "fetch_all_releases", lambda: releases)
    monkeypatch.setattr(sys, "argv", ["sync-releases.py", "--dry-run", "--test", "3"])
    mod.main()
    out = capsys.readouterr().out
    assert "Test mode: 3 releases only" in out
    assert "DRY RUN: 3 releases" in out


def test_test_flag_default_is_5(monkeypatch):
    """--test with no following number defaults to 5 (current behavior across
    scripts that support --test)."""
    mod = load_script("sync-docs.py")
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "k")
    monkeypatch.setattr(mod, "list_all_docs", lambda: [f"docs/f{i}.md" for i in range(20)])
    captured = {}

    # Replace dry-run path so we can read test_limit indirectly via output.
    monkeypatch.setattr(sys, "argv", ["sync-docs.py", "--dry-run", "--test"])
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.main()
    assert "Test mode: 5 files only" in buf.getvalue()


# ===========================================================================
# Batching of 5 + failure counting (the retained/failed loop)
# ===========================================================================

def test_releases_batches_in_groups_of_five(monkeypatch, capsys):
    """13 valid releases -> retain_batch called with sizes [5,5,3]."""
    mod = load_script("sync-releases.py")
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "k")
    releases = [
        {"tag_name": f"n8n@1.0.{i}", "published_at": "2024-01-01T00:00:00Z",
         "body": "body text long enough", "html_url": f"http://r{i}"}
        for i in range(13)
    ]
    monkeypatch.setattr(mod, "fetch_all_releases", lambda: releases)
    monkeypatch.setattr(mod, "save_state", lambda s: None)
    monkeypatch.setattr(mod, "load_state", lambda: {})

    batch_sizes = []

    def fake_retain(items):
        batch_sizes.append(len(items))
        return True

    monkeypatch.setattr(mod, "retain_batch", fake_retain)
    monkeypatch.setattr(sys, "argv", ["sync-releases.py", "--full"])
    mod.main()
    assert batch_sizes == [5, 5, 3]
    assert "13 retained" in capsys.readouterr().out


def test_releases_failed_batch_counts_as_failed(monkeypatch, capsys):
    """When retain_batch returns False, those items count as failed, not
    retained, and the loop continues."""
    mod = load_script("sync-releases.py")
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "k")
    releases = [
        {"tag_name": f"n8n@1.0.{i}", "published_at": "2024-01-01T00:00:00Z",
         "body": "body text long enough", "html_url": f"http://r{i}"}
        for i in range(5)
    ]
    monkeypatch.setattr(mod, "fetch_all_releases", lambda: releases)
    monkeypatch.setattr(mod, "save_state", lambda s: None)
    monkeypatch.setattr(mod, "load_state", lambda: {})
    monkeypatch.setattr(mod, "retain_batch", lambda items: False)
    monkeypatch.setattr(sys, "argv", ["sync-releases.py", "--full"])
    mod.main()
    out = capsys.readouterr().out
    assert "0 retained" in out
    assert "5 failed" in out


def test_releases_skips_short_body(monkeypatch, capsys):
    """format_release returns None for bodies < 10 chars -> counted skipped."""
    mod = load_script("sync-releases.py")
    monkeypatch.setattr(mod, "HINDSIGHT_KEY", "k")
    releases = [
        {"tag_name": "n8n@1.0.0", "published_at": "2024-01-01T00:00:00Z",
         "body": "short", "html_url": "http://r"},  # < 10 chars -> skip
        {"tag_name": "n8n@1.0.1", "published_at": "2024-01-01T00:00:00Z",
         "body": "long enough body", "html_url": "http://r2"},
    ]
    monkeypatch.setattr(mod, "fetch_all_releases", lambda: releases)
    monkeypatch.setattr(mod, "save_state", lambda s: None)
    monkeypatch.setattr(mod, "load_state", lambda: {})
    monkeypatch.setattr(mod, "retain_batch", lambda items: True)
    monkeypatch.setattr(sys, "argv", ["sync-releases.py", "--full"])
    mod.main()
    out = capsys.readouterr().out
    assert "1 retained" in out
    assert "1 skipped" in out
