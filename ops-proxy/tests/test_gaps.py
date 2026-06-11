"""Tests for coverage gaps: fallback limiter and forward-mode network failures."""
import asyncio
import importlib
import json
from unittest.mock import patch, MagicMock
import urllib.error

import pytest
from fastapi.testclient import TestClient


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

    async def start(self):
        self.start_calls += 1

    async def close(self):
        self.close_calls += 1

    async def validate(self, workflow):
        return {
            "valid": True,
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
            "statistics": {"totalNodes": 1, "triggerNodes": 1},
            "suggestions": [],
        }


# ============================================================================
# Gap 1: Real slowapi limiter path - test the fallback Limiter class directly
# ============================================================================


class TestFallbackLimiter:
    """Test the fallback Limiter class used when slowapi is missing."""

    def test_fallback_limiter_allows_requests_within_limit(self):
        """Fallback limiter should allow requests up to the max_calls threshold."""
        from app import Limiter, RateLimitExceeded, get_remote_address

        limiter = Limiter(key_func=get_remote_address)

        # Create a simple mock Request with client.host
        mock_request = MagicMock()
        mock_request.client.host = "192.168.1.1"

        # Decorate a function with a 3/minute limit
        @limiter.limit("3/minute")
        async def test_endpoint(request):
            return {"status": "ok"}

        # Should allow exactly 3 calls
        for i in range(3):
            # Call with request in kwargs
            result = asyncio.run(test_endpoint(request=mock_request))
            assert result == {"status": "ok"}

    def test_fallback_limiter_rejects_requests_exceeding_limit(self):
        """Fallback limiter should raise RateLimitExceeded when limit is exceeded."""
        import asyncio
        from app import Limiter, RateLimitExceeded, get_remote_address

        limiter = Limiter(key_func=get_remote_address)

        mock_request = MagicMock()
        mock_request.client.host = "192.168.1.1"

        @limiter.limit("2/minute")
        async def test_endpoint(request):
            return {"status": "ok"}

        # Allow 2 calls
        for i in range(2):
            result = asyncio.run(test_endpoint(request=mock_request))
            assert result == {"status": "ok"}

        # Third call should raise RateLimitExceeded
        with pytest.raises(RateLimitExceeded):
            asyncio.run(test_endpoint(request=mock_request))

    def test_fallback_limiter_disabled_allows_unlimited_requests(self):
        """When limiter.enabled=False, requests should bypass the limit."""
        import asyncio
        from app import Limiter, get_remote_address

        limiter = Limiter(key_func=get_remote_address)
        limiter.enabled = False

        mock_request = MagicMock()
        mock_request.client.host = "192.168.1.1"

        @limiter.limit("1/minute")
        async def test_endpoint(request):
            return {"status": "ok"}

        # Should allow unlimited calls when disabled
        for i in range(10):
            result = asyncio.run(test_endpoint(request=mock_request))
            assert result == {"status": "ok"}

    def test_fallback_limiter_tracks_per_client(self):
        """Limiter should track requests per-client separately."""
        import asyncio
        from app import Limiter, RateLimitExceeded, get_remote_address

        limiter = Limiter(key_func=get_remote_address)

        mock_request_a = MagicMock()
        mock_request_a.client.host = "192.168.1.1"

        mock_request_b = MagicMock()
        mock_request_b.client.host = "192.168.1.2"

        @limiter.limit("1/minute")
        async def test_endpoint(request):
            return {"status": "ok"}

        # Client A: 1 call succeeds
        result = asyncio.run(test_endpoint(request=mock_request_a))
        assert result == {"status": "ok"}

        # Client A: 2nd call fails
        with pytest.raises(RateLimitExceeded):
            asyncio.run(test_endpoint(request=mock_request_a))

        # Client B: 1 call succeeds (separate bucket)
        result = asyncio.run(test_endpoint(request=mock_request_b))
        assert result == {"status": "ok"}

        # Client B: 2nd call fails
        with pytest.raises(RateLimitExceeded):
            asyncio.run(test_endpoint(request=mock_request_b))

    def test_fallback_limiter_reset(self):
        """Limiter.reset() should clear all counts."""
        import asyncio
        from app import Limiter, RateLimitExceeded, get_remote_address

        limiter = Limiter(key_func=get_remote_address)

        mock_request = MagicMock()
        mock_request.client.host = "192.168.1.1"

        @limiter.limit("1/minute")
        async def test_endpoint(request):
            return {"status": "ok"}

        # Use up the limit
        asyncio.run(test_endpoint(request=mock_request))
        with pytest.raises(RateLimitExceeded):
            asyncio.run(test_endpoint(request=mock_request))

        # Reset and try again
        limiter.reset()
        result = asyncio.run(test_endpoint(request=mock_request))
        assert result == {"status": "ok"}


# ============================================================================
# Gap 2: Forward-mode network failures
# ============================================================================


@pytest.fixture
def forward_mode_client(monkeypatch):
    """Create a FastAPI client in forward mode (no local validator)."""
    monkeypatch.setenv(
        "WORKFLOW_VALIDATOR_FORWARD_URL",
        "https://validator.example/public/validate-workflow",
    )
    monkeypatch.setenv("WORKFLOW_VALIDATOR_FORWARD_TIMEOUT_SECONDS", "5")

    import workflow_validator
    import app as app_module

    def _unexpected_bridge():
        raise AssertionError("local validator should not be constructed in forward mode")

    monkeypatch.setattr(workflow_validator, "build_validator_bridge", _unexpected_bridge)
    monkeypatch.setattr(workflow_validator, "get_validator_metadata", lambda: dict(FAKE_VALIDATOR_INFO))
    app_module = importlib.reload(app_module)
    fastapi_app = app_module.create_app()
    lim = getattr(fastapi_app.state, "limiter", None)
    if lim is not None:
        lim.enabled = False

    with TestClient(fastapi_app) as client:
        # Return both client and app_module for use in tests
        yield client, app_module


def test_forward_mode_connection_refused_returns_503(forward_mode_client, monkeypatch):
    """When forward target refuses connection, should return 503 with JSON error."""
    import workflow_validator
    client, app_module = forward_mode_client

    async def _mock_forward_connection_refused(body: bytes):
        # Simulate URLError (connection refused) being wrapped in WorkflowValidatorUnavailable
        raise workflow_validator.WorkflowValidatorUnavailable("Connection refused")

    monkeypatch.setattr(app_module, "_forward_validation_request", _mock_forward_connection_refused)

    payload = {
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
    }

    response = client.post("/public/validate-workflow", json=payload)

    # Should return 503, not 500 or raw stack trace
    assert response.status_code == 503
    # Should return JSON error, not HTML or raw text
    data = response.json()
    assert "detail" in data
    assert isinstance(data["detail"], str)
    assert "Connection refused" in data["detail"]


def test_forward_mode_timeout_returns_503(forward_mode_client, monkeypatch):
    """When forward target times out, should return 503 with JSON error."""
    import workflow_validator
    client, app_module = forward_mode_client

    async def _mock_forward_timeout(body: bytes):
        # Simulate timeout wrapped in WorkflowValidatorUnavailable
        raise workflow_validator.WorkflowValidatorUnavailable("timed out")

    monkeypatch.setattr(app_module, "_forward_validation_request", _mock_forward_timeout)

    payload = {
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
    }

    response = client.post("/public/validate-workflow", json=payload)

    assert response.status_code == 503
    data = response.json()
    assert "detail" in data
    assert "timed out" in data["detail"]


def test_forward_mode_downstream_500_returns_503(forward_mode_client, monkeypatch):
    """When forward target returns 500, should return 503 (not 500)."""
    import workflow_validator
    client, app_module = forward_mode_client

    async def _mock_forward_500(body: bytes):
        # Simulate downstream 500 wrapped in WorkflowValidatorUnavailable
        raise workflow_validator.WorkflowValidatorUnavailable("Downstream validator returned 500: Internal Server Error")

    monkeypatch.setattr(app_module, "_forward_validation_request", _mock_forward_500)

    payload = {
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
    }

    response = client.post("/public/validate-workflow", json=payload)

    # Downstream 500 should be converted to 503 by the proxy
    assert response.status_code == 503
    data = response.json()
    assert "detail" in data


def test_forward_mode_invalid_json_response_returns_503(forward_mode_client, monkeypatch):
    """When forward target returns invalid JSON, should return 503 with JSON error."""
    import workflow_validator
    client, app_module = forward_mode_client

    async def _mock_forward_invalid_json(body: bytes):
        # Simulate unparseable response wrapped in WorkflowValidatorUnavailable
        raise workflow_validator.WorkflowValidatorUnavailable("Invalid JSON in response")

    monkeypatch.setattr(app_module, "_forward_validation_request", _mock_forward_invalid_json)

    payload = {
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
    }

    response = client.post("/public/validate-workflow", json=payload)

    assert response.status_code == 503
    data = response.json()
    assert "detail" in data
    assert "Invalid JSON" in data["detail"]


def test_forward_mode_downstream_unavailable_returns_503(forward_mode_client, monkeypatch):
    """When _forward_validation_request raises WorkflowValidatorUnavailable, should return 503."""
    client, app_module = forward_mode_client
    import workflow_validator

    async def _mock_forward_unavailable(body: bytes):
        raise workflow_validator.WorkflowValidatorUnavailable("Validator is down for maintenance")

    monkeypatch.setattr(app_module, "_forward_validation_request", _mock_forward_unavailable)

    payload = {
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
    }

    response = client.post("/public/validate-workflow", json=payload)

    assert response.status_code == 503
    data = response.json()
    assert "detail" in data
    assert "Validator is down" in data["detail"]


def test_forward_mode_successful_response_proxied_as_is(forward_mode_client, monkeypatch):
    """When forward target returns 200, response should be proxied as-is."""
    client, app_module = forward_mode_client
    from starlette.responses import JSONResponse

    async def _mock_forward_success(body: bytes):
        return JSONResponse(
            {"valid": True, "forwarded": True},
            status_code=200,
        )

    monkeypatch.setattr(app_module, "_forward_validation_request", _mock_forward_success)

    payload = {
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
    }

    response = client.post("/public/validate-workflow", json=payload)

    assert response.status_code == 200
    assert response.json() == {"valid": True, "forwarded": True}
