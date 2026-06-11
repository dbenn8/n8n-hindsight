"""Unit tests for the extracted sync_common module."""
import json
import os
import sys

import pytest

# sync_common is importable because conftest puts SCRIPTS_DIR on sys.path.
import sync_common


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


# --- constants ---

def test_constants():
    assert sync_common.BATCH_SIZE == 5
    assert sync_common.RETAIN_SLEEP == 0.5
    assert sync_common.HTTP_TIMEOUT == 120
    assert sync_common.BANK_ID == "n8n"
    assert sync_common.RETAIN_OK_STATUSES == (200, 201, 202)


# --- resolve_env ---

def test_resolve_env_defaults(monkeypatch):
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    monkeypatch.delenv("HINDSIGHT_API_TENANT_API_KEY", raising=False)
    url, key = sync_common.resolve_env()
    assert url == "http://127.0.0.1:8889"
    assert key == ""


def test_resolve_env_overrides(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_URL", "https://h.example")
    monkeypatch.setenv("HINDSIGHT_API_TENANT_API_KEY", "abc")
    url, key = sync_common.resolve_env()
    assert url == "https://h.example"
    assert key == "abc"


# --- load_state / save_state ---

def test_load_state_missing(tmp_path):
    assert sync_common.load_state(str(tmp_path / "x.json")) == {}


def test_save_load_roundtrip_creates_dirs(tmp_path):
    sf = tmp_path / "a" / "b" / "state.json"
    sync_common.save_state(str(sf), {"k": 1})
    assert sf.exists()
    assert sync_common.load_state(str(sf)) == {"k": 1}
    assert "  " in sf.read_text()  # indent=2


def test_save_state_bare_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sync_common.save_state("bare.json", {"k": 1})
    assert (tmp_path / "bare.json").exists()


# --- retain_batch ---

def test_retain_batch_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return FakeResponse(202)

    monkeypatch.setattr(sync_common.urllib.request, "urlopen", fake_urlopen)
    ok = sync_common.retain_batch([{"x": 1}], "http://h", "key", "n8n")
    assert ok is True
    assert captured["url"] == "http://h/v1/default/banks/n8n/memories"
    assert captured["data"] == {"items": [{"x": 1}], "async": True}
    assert captured["timeout"] == 120
    assert captured["headers"]["authorization"] == "Bearer key"
    assert captured["headers"]["content-type"] == "application/json"


@pytest.mark.parametrize("status,expected",
                         [(200, True), (201, True), (202, True), (204, False), (500, False)])
def test_retain_batch_status(monkeypatch, status, expected):
    monkeypatch.setattr(sync_common.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResponse(status))
    assert sync_common.retain_batch([{}], "http://h", "k") is expected


def test_retain_batch_exception(monkeypatch, capsys):
    def boom(req, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(sync_common.urllib.request, "urlopen", boom)
    assert sync_common.retain_batch([{}], "http://h", "k") is False
    err = capsys.readouterr().err
    assert "RETAIN ERROR" in err and "refused" in err


def test_retain_batch_default_bank(monkeypatch):
    captured = {}
    monkeypatch.setattr(sync_common.urllib.request, "urlopen",
                        lambda req, timeout=None: captured.setdefault("u", req.full_url) or FakeResponse(200))
    sync_common.retain_batch([{}], "http://h", "k")
    assert "/banks/n8n/" in captured["u"]


# --- build_arg_parser ---

def test_arg_parser_basic_flags():
    p = sync_common.build_arg_parser()
    a = p.parse_args(["--full", "--dry-run", "--test", "7"])
    assert a.full is True and a.dry_run is True and a.test == 7


def test_arg_parser_test_bare_defaults_to_5():
    p = sync_common.build_arg_parser()
    a = p.parse_args(["--test"])
    assert a.test == 5


def test_arg_parser_test_absent_is_none():
    p = sync_common.build_arg_parser()
    a = p.parse_args([])
    assert a.test is None
    assert a.full is False and a.dry_run is False


def test_arg_parser_can_omit_test():
    p = sync_common.build_arg_parser(test=False)
    assert not hasattr(p.parse_args([]), "test")


def test_arg_parser_no_abbreviation():
    """allow_abbrev=False: a prefix of a flag is treated as unknown, not a
    silent match (preserves the original exact-string flag checks)."""
    p = sync_common.build_arg_parser()
    _, unknown = p.parse_known_args(["--dr"])
    assert "--dr" in unknown
