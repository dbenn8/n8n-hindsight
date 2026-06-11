import importlib
import json

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

FAKE_VALIDATOR_INFO = {
    "validator_engine": "n8n-mcp",
    "configured_n8n_mcp_version": "2.57.3",
    "installed_n8n_mcp_version": "2.57.3",
    "nodes_db_sha256": "abc123",
}


class FakeValidatorBridge:
    def __init__(self):
        self.start_calls = 0
        self.close_calls = 0
        self.calls = []

    async def start(self):
        self.start_calls += 1

    async def close(self):
        self.close_calls += 1

    async def validate(self, workflow):
        self.calls.append(workflow)
        node_names = [node.get("name") for node in workflow.get("nodes", [])]
        if "Broken" in node_names:
            return {
                "valid": False,
                "error_count": 3,
                "warning_count": 1,
                "errors": [
                    {"type": "schema_error", "message": "Required property 'Name' cannot be empty", "node": "Broken"},
                    {"type": "schema_error", "message": "Required property 'Name' cannot be empty", "node": "Broken"},
                    {"type": "expression_error", "message": "Expression format error in node Broken", "node": "Broken"},
                ],
                "warnings": [
                    {"type": "warning", "message": "A warning", "node": "Broken"},
                ],
                "statistics": {"totalNodes": len(workflow.get("nodes", [])), "triggerNodes": 1},
                "suggestions": ["Fix the required Name field"],
            }
        return {
            "valid": True,
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
            "statistics": {"totalNodes": len(workflow.get("nodes", [])), "triggerNodes": 1},
            "suggestions": [],
        }


@pytest.fixture
def validator_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOW_VALIDATOR_MAX_BODY_BYTES", "512")
    fake_validator = FakeValidatorBridge()

    import workflow_validator
    import app as app_module

    monkeypatch.setattr(workflow_validator, "build_validator_bridge", lambda: fake_validator)
    monkeypatch.setattr(workflow_validator, "get_validator_metadata", lambda: dict(FAKE_VALIDATOR_INFO))
    app_module = importlib.reload(app_module)
    fastapi_app = app_module.create_app()
    lim = getattr(fastapi_app.state, "limiter", None)
    if lim is not None:
        lim.enabled = False

    with TestClient(fastapi_app) as client:
        yield client, fake_validator


def test_raw_workflow_json_returns_valid_summary(validator_client):
    client, fake_validator = validator_client
    payload = {
        "nodes": [
            {
                "id": "1",
                "name": "Manual Trigger",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {},
            }
        ],
        "connections": {},
    }

    response = client.post("/public/validate-workflow", json=payload)

    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["has_json"] is True
    assert response.json()["node_count"] == 1
    assert response.json()["validator_info"] == FAKE_VALIDATOR_INFO
    assert fake_validator.start_calls == 1
    assert len(fake_validator.calls) == 1


def test_health_returns_local_validator_info(validator_client):
    client, _ = validator_client

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "validator_mode": "local",
        "validator_info": FAKE_VALIDATOR_INFO,
    }


def test_public_validator_health_matches_local_health(validator_client):
    client, _ = validator_client

    response = client.get("/public/validator-health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "validator_mode": "local",
        "validator_info": FAKE_VALIDATOR_INFO,
    }


def test_invalid_workflow_returns_deduped_repair_messages(validator_client):
    client, _ = validator_client
    payload = {
        "workflow": {
            "nodes": [
                {
                    "id": "1",
                    "name": "Broken",
                    "type": "n8n-nodes-base.httpRequest",
                    "typeVersion": 1,
                    "position": [0, 0],
                    "parameters": {},
                }
            ],
            "connections": {},
        },
        "max_errors": 8,
    }

    response = client.post("/public/validate-workflow", json=payload)
    body = response.json()

    assert response.status_code == 200
    assert body["valid"] is False
    assert body["error_count"] == 3
    assert body["warning_count"] == 1
    assert body["repair_messages"] == [
        "Required property 'Name' cannot be empty",
        "Expression format error in node Broken",
    ]
    assert body["feedback_block"].startswith("- Required property 'Name' cannot be empty")


def test_workflow_payload_preserves_raw_validator_error_objects(validator_client):
    client, _ = validator_client
    payload = {
        "workflow": {
            "nodes": [
                {
                    "id": "1",
                    "name": "Broken",
                    "type": "n8n-nodes-base.httpRequest",
                    "typeVersion": 1,
                    "position": [0, 0],
                    "parameters": {},
                }
            ],
            "connections": {},
        },
    }

    response = client.post("/public/validate-workflow", json=payload)
    body = response.json()

    assert response.status_code == 200
    assert body["errors"] == [
        {"type": "schema_error", "message": "Required property 'Name' cannot be empty", "node": "Broken"},
        {"type": "schema_error", "message": "Required property 'Name' cannot be empty", "node": "Broken"},
        {"type": "expression_error", "message": "Expression format error in node Broken", "node": "Broken"},
    ]
    assert body["warnings"] == [
        {"type": "warning", "message": "A warning", "node": "Broken"},
    ]
    assert "issues" not in body
    assert "repair_prompt" not in body


def test_prose_only_response_returns_no_json_feedback(validator_client):
    client, _ = validator_client

    response = client.post(
        "/public/validate-workflow",
        json={"response_text": "Here is a great workflow plan, but no JSON yet."},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["valid"] is False
    assert body["has_json"] is False
    assert body["extract_error"] == "no_json_found"
    assert body["repair_messages"][0].startswith("Return a single complete importable")


def test_fenced_json_extraction_works(validator_client):
    client, fake_validator = validator_client
    workflow = {
        "nodes": [
            {
                "id": "1",
                "name": "Manual Trigger",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {},
            }
        ],
        "connections": {},
    }
    response_text = f"Use this:\n```json\n{json.dumps(workflow)}\n```"

    response = client.post(
        "/public/validate-workflow",
        json={"response_text": response_text},
    )

    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert len(fake_validator.calls) == 1


def test_bare_embedded_json_extraction_works(validator_client):
    client, fake_validator = validator_client
    workflow = {
        "nodes": [
            {
                "id": "1",
                "name": "Manual Trigger",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {"notes": "x" * 220},
            }
        ],
        "connections": {},
    }
    response_text = f"Here is the workflow draft: {json.dumps(workflow)} End of response."

    response = client.post(
        "/public/validate-workflow",
        json={"response_text": response_text},
    )

    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert len(fake_validator.calls) == 1


def test_include_repair_prompt_returns_prompt(validator_client):
    client, _ = validator_client
    response = client.post(
        "/public/validate-workflow",
        json={
            "workflow": {
                "nodes": [
                    {
                        "id": "1",
                        "name": "Broken",
                        "type": "n8n-nodes-base.httpRequest",
                        "typeVersion": 1,
                        "position": [0, 0],
                        "parameters": {},
                    }
                ],
                "connections": {},
            },
            "original_prompt": "Build an n8n workflow that posts to Slack.",
            "include_repair_prompt": True,
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert "repair_prompt" not in body


def test_large_request_body_rejected_cleanly(validator_client):
    client, _ = validator_client
    response = client.post(
        "/public/validate-workflow",
        content=json.dumps({"response_text": "x" * 5000}),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Request body too large"


def test_malformed_json_body_returns_400(validator_client):
    client, _ = validator_client
    response = client.post(
        "/public/validate-workflow",
        content='{"response_text": "oops"',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Malformed JSON body"


def test_forward_mode_proxies_without_starting_local_validator(monkeypatch):
    monkeypatch.setenv(
        "WORKFLOW_VALIDATOR_FORWARD_URL",
        "https://validator.example/public/validate-workflow",
    )

    import workflow_validator
    import app as app_module

    def _unexpected_bridge():
        raise AssertionError("local validator should not be constructed in forward mode")

    async def _fake_forward(body: bytes):
        payload = json.loads(body)
        assert payload["workflow"]["nodes"][0]["name"] == "Manual Trigger"
        return JSONResponse({"valid": True, "forwarded": True})

    monkeypatch.setattr(workflow_validator, "build_validator_bridge", _unexpected_bridge)
    monkeypatch.setattr(workflow_validator, "get_validator_metadata", lambda: dict(FAKE_VALIDATOR_INFO))
    app_module = importlib.reload(app_module)
    monkeypatch.setattr(app_module, "_forward_validation_request", _fake_forward)

    fastapi_app = app_module.create_app()
    lim = getattr(fastapi_app.state, "limiter", None)
    if lim is not None:
        lim.enabled = False

    with TestClient(fastapi_app) as client:
        response = client.post(
            "/public/validate-workflow",
            json={
                "workflow": {
                    "nodes": [
                        {
                            "id": "1",
                            "name": "Manual Trigger",
                            "type": "n8n-nodes-base.manualTrigger",
                            "typeVersion": 1,
                            "position": [0, 0],
                            "parameters": {},
                        }
                    ],
                    "connections": {},
                }
            },
        )

    assert response.status_code == 200
    assert response.json() == {"valid": True, "forwarded": True}


def test_forward_mode_health_reports_downstream_validator_info(monkeypatch):
    monkeypatch.setenv(
        "WORKFLOW_VALIDATOR_FORWARD_URL",
        "https://validator.example/public/validate-workflow",
    )

    import workflow_validator
    import app as app_module

    def _unexpected_bridge():
        raise AssertionError("local validator should not be constructed in forward mode")

    async def _fake_forward_health():
        return {
            "status": "ok",
            "validator_mode": "local",
            "validator_info": {
                "validator_engine": "n8n-mcp",
                "configured_n8n_mcp_version": "2.57.3",
                "installed_n8n_mcp_version": "2.57.3",
                "nodes_db_sha256": "remote456",
            },
        }

    monkeypatch.setattr(workflow_validator, "build_validator_bridge", _unexpected_bridge)
    monkeypatch.setattr(workflow_validator, "get_validator_metadata", lambda: dict(FAKE_VALIDATOR_INFO))
    app_module = importlib.reload(app_module)
    monkeypatch.setattr(app_module, "_fetch_forward_health", _fake_forward_health)

    fastapi_app = app_module.create_app()
    lim = getattr(fastapi_app.state, "limiter", None)
    if lim is not None:
        lim.enabled = False

    with TestClient(fastapi_app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "validator_mode": "forward",
        "validator_forward_url": "https://validator.example/public/validate-workflow",
        "forward_status": "ok",
        "forward_validator_mode": "local",
        "validator_info": {
            "validator_engine": "n8n-mcp",
            "configured_n8n_mcp_version": "2.57.3",
            "installed_n8n_mcp_version": "2.57.3",
            "nodes_db_sha256": "remote456",
        },
    }
