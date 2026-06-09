import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "hindsight-api.log").write_text(
        "2026 consolidation ok\n2026 GREENPT fallback 401\n2026 recall done\n")
    monkeypatch.setenv("LOGS_ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("LOGS_DIR", str(logs))
    import workflow_validator
    import app as app_module

    class FakeValidatorBridge:
        async def start(self):
            return None

        async def close(self):
            return None

        async def validate(self, workflow):
            return {"valid": True, "error_count": 0, "warning_count": 0, "errors": [], "warnings": [], "statistics": {}}

    monkeypatch.setattr(workflow_validator, "build_validator_bridge", lambda: FakeValidatorBridge())
    app_module = importlib.reload(app_module)
    fastapi_app = app_module.create_app()
    lim = getattr(fastapi_app.state, "limiter", None)
    if lim is not None:
        lim.enabled = False
    with TestClient(fastapi_app) as test_client:
        yield test_client


def test_invalid_grep_pattern_400(client):
    r = client.get("/logs?service=hindsight-api&grep=(unbalanced",
                   headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 400


def test_oversized_grep_400(client):
    r = client.get("/logs?service=hindsight-api&grep=" + "a" * 500,
                   headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 400


def test_rate_limited(tmp_path, monkeypatch):
    logs = tmp_path / "logs"; logs.mkdir()
    (logs / "hindsight-api.log").write_text("line\n")
    monkeypatch.setenv("LOGS_ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("LOGS_DIR", str(logs))
    import workflow_validator
    import app as app_module

    class FakeValidatorBridge:
        async def start(self):
            return None

        async def close(self):
            return None

        async def validate(self, workflow):
            return {"valid": True, "error_count": 0, "warning_count": 0, "errors": [], "warnings": [], "statistics": {}}

    monkeypatch.setattr(workflow_validator, "build_validator_bridge", lambda: FakeValidatorBridge())
    app_module = importlib.reload(app_module)
    fastapi_app = app_module.create_app()
    fastapi_app.state.limiter.enabled = True
    fastapi_app.state.limiter.reset()
    h = {"Authorization": "Bearer admin-secret"}
    with TestClient(fastapi_app) as client:
        codes = [client.get("/logs?service=hindsight-api", headers=h).status_code
                 for _ in range(35)]
    assert 429 in codes  # exceeds 30/minute


def test_requires_admin_key(client):
    assert client.get("/logs?service=hindsight-api").status_code == 401


def test_rejects_wrong_key(client):
    r = client.get("/logs?service=hindsight-api",
                   headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_returns_log_with_admin_key(client):
    r = client.get("/logs?service=hindsight-api",
                   headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200 and "consolidation ok" in r.text


def test_grep_filters(client):
    r = client.get("/logs?service=hindsight-api&grep=GREENPT",
                   headers={"Authorization": "Bearer admin-secret"})
    assert "GREENPT" in r.text and "consolidation ok" not in r.text


def test_lines_tail(client):
    r = client.get("/logs?service=hindsight-api&lines=1",
                   headers={"Authorization": "Bearer admin-secret"})
    assert r.text.strip() == "2026 recall done"


def test_blocks_path_traversal(client):
    r = client.get("/logs?service=../../etc/passwd",
                   headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 400
