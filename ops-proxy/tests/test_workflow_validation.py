import importlib
import json

import pytest
from fastapi.testclient import TestClient


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
    assert fake_validator.start_calls == 1
    assert len(fake_validator.calls) == 1


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
    assert "repair_prompt" in body
    assert "Original user request:" in body["repair_prompt"]
    assert "Build an n8n workflow that posts to Slack." in body["repair_prompt"]
    assert "Current workflow draft JSON:" in body["repair_prompt"]


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
