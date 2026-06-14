import hashlib
import importlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest
from fastapi.testclient import TestClient


FAKE_MODELS = {
    "items": [
        {
            "id": "mm-1",
            "name": "Google Sheets Known Issues",
            "tags": ["tag:google-sheets"],
            "content": "## Google Sheets\n- Bug: date parsing fails on EU locales",
            "last_refreshed_at": "2026-06-14T09:26:17+00:00",
        },
        {
            "id": "mm-2",
            "name": "Slack Known Issues",
            "tags": ["tag:slack"],
            "content": "## Slack\n- Bug: file upload >5MB silently fails",
            "last_refreshed_at": "2026-06-14T09:37:52+00:00",
        },
        {
            "id": "mm-3",
            "name": "Generating Placeholder",
            "tags": ["tag:placeholder"],
            "content": "Generating content...",
            "last_refreshed_at": "2026-06-14T09:00:00+00:00",
        },
    ]
}


class FakeHindsightHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(FAKE_MODELS).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def hindsight_server():
    server = HTTPServer(("127.0.0.1", 0), FakeHindsightHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def client(monkeypatch, hindsight_server):
    monkeypatch.setenv("HINDSIGHT_API_URL", hindsight_server)
    monkeypatch.setenv("HINDSIGHT_API_TENANT_API_KEY", "test-key")
    monkeypatch.setenv("MANIFEST_CACHE_TTL_SECONDS", "0")
    import workflow_validator
    import app as app_module

    class FakeValidatorBridge:
        async def start(self):
            return None
        async def close(self):
            return None
        async def validate(self, workflow):
            return {"valid": True}

    monkeypatch.setattr(workflow_validator, "build_validator_bridge", lambda: FakeValidatorBridge())
    app_module._manifest_cache = None
    app_module._manifest_cache_at = 0
    app_module = importlib.reload(app_module)
    fastapi_app = app_module.create_app()
    with TestClient(fastapi_app) as c:
        yield c


def test_manifest_returns_models_with_hashes(client):
    resp = client.get("/public/mental-models/manifest")
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert "google-sheets" in data["models"]
    assert "slack" in data["models"]
    gs = data["models"]["google-sheets"]
    expected_hash = hashlib.sha256(
        FAKE_MODELS["items"][0]["content"].encode("utf-8")
    ).hexdigest()
    assert gs["content_hash"] == expected_hash
    assert gs["size"] == len(FAKE_MODELS["items"][0]["content"])
    assert gs["last_refreshed_at"] == "2026-06-14T09:26:17+00:00"


def test_manifest_excludes_generating_placeholder(client):
    resp = client.get("/public/mental-models/manifest")
    data = resp.json()
    assert "placeholder" not in data["models"]


def test_manifest_caching(client, monkeypatch):
    resp1 = client.get("/public/mental-models/manifest")
    assert resp1.status_code == 200
    import app as app_module
    monkeypatch.setattr(app_module, "_manifest_cache_at", app_module.time.monotonic())
    monkeypatch.setenv("MANIFEST_CACHE_TTL_SECONDS", "9999")
    resp2 = client.get("/public/mental-models/manifest")
    assert resp2.status_code == 200
    assert resp1.json() == resp2.json()
