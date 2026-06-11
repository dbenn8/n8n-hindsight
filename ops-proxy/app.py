"""Standalone admin + workflow validation service."""
import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

import workflow_validator

try:
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address
except ModuleNotFoundError:
    class RateLimitExceeded(Exception):
        """Fallback rate-limit exception for local test environments."""

    def get_remote_address(request: Request) -> str:
        client = getattr(request, "client", None)
        return getattr(client, "host", None) or "local"

    class Limiter:
        """Tiny in-memory limiter used only when slowapi is unavailable."""

        def __init__(self, key_func):
            self.enabled = True
            self.key_func = key_func
            self._counts: dict[tuple[str, str], int] = {}

        def limit(self, rule: str):
            max_calls = int(rule.split("/", 1)[0])

            def decorator(func):
                @wraps(func)
                async def wrapper(*args, **kwargs):
                    if self.enabled:
                        request = kwargs.get("request")
                        if request is None:
                            request = next(
                                (
                                    arg for arg in args
                                    if isinstance(arg, Request)
                                ),
                                None,
                            )
                        key = self.key_func(request) if request is not None else "local"
                        bucket = (func.__name__, key)
                        count = self._counts.get(bucket, 0) + 1
                        self._counts[bucket] = count
                        if count > max_calls:
                            raise RateLimitExceeded()
                    return await func(*args, **kwargs)

                return wrapper

            return decorator

        def reset(self):
            self._counts.clear()

_SERVICE_RE = re.compile(r"^[a-z0-9-]+$")
_MAX_GREP_LEN = 200
_MAX_LINES = 10_000
_DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
_DEFAULT_FORWARD_TIMEOUT_SECONDS = 30
_DEFAULT_FORWARD_HEALTH_TIMEOUT_SECONDS = 5

limiter = Limiter(key_func=get_remote_address)


def _max_request_body_bytes() -> int:
    raw = os.environ.get("WORKFLOW_VALIDATOR_MAX_BODY_BYTES", "")
    try:
        value = int(raw) if raw else _DEFAULT_MAX_REQUEST_BODY_BYTES
    except ValueError:
        value = _DEFAULT_MAX_REQUEST_BODY_BYTES
    return max(1_024, value)


def _validator_forward_url() -> str | None:
    raw = os.environ.get("WORKFLOW_VALIDATOR_FORWARD_URL", "").strip()
    return raw or None


def _validator_forward_timeout_seconds() -> int:
    raw = os.environ.get("WORKFLOW_VALIDATOR_FORWARD_TIMEOUT_SECONDS", "")
    try:
        value = int(raw) if raw else _DEFAULT_FORWARD_TIMEOUT_SECONDS
    except ValueError:
        value = _DEFAULT_FORWARD_TIMEOUT_SECONDS
    return max(1, value)


def _validator_forward_health_url() -> str | None:
    validate_url = _validator_forward_url()
    if not validate_url:
        return None

    parsed = urllib.parse.urlsplit(validate_url)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/health", "", "")
    )


async def _forward_validation_request(body: bytes) -> Response:
    url = _validator_forward_url()
    if not url:
        raise workflow_validator.WorkflowValidatorUnavailable(
            "Workflow validator forward URL is not configured"
        )

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _post() -> tuple[int, str, bytes]:
        try:
            with urllib.request.urlopen(
                req,
                timeout=_validator_forward_timeout_seconds(),
            ) as resp:
                return (
                    resp.status,
                    resp.headers.get("Content-Type", "application/json"),
                    resp.read(),
                )
        except urllib.error.HTTPError as exc:
            return (
                exc.code,
                exc.headers.get("Content-Type", "application/json"),
                exc.read(),
            )

    try:
        status_code, content_type, payload = await asyncio.to_thread(_post)
    except Exception as exc:
        raise workflow_validator.WorkflowValidatorUnavailable(
            f"Workflow validator forward request failed: {exc}"
        ) from exc

    media_type = content_type.split(";", 1)[0] if content_type else "application/json"
    return Response(content=payload, status_code=status_code, media_type=media_type)


async def _fetch_forward_health() -> dict | None:
    url = _validator_forward_health_url()
    if not url:
        return None

    req = urllib.request.Request(url, method="GET")

    def _get() -> dict:
        with urllib.request.urlopen(
            req,
            timeout=min(
                _validator_forward_timeout_seconds(),
                _DEFAULT_FORWARD_HEALTH_TIMEOUT_SECONDS,
            ),
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        payload = await asyncio.to_thread(_get)
    except Exception as exc:
        return {
            "status": "unavailable",
            "detail": str(exc),
        }

    return payload if isinstance(payload, dict) else None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.workflow_validator_forward_url = _validator_forward_url()
    app.state.workflow_validator_start_error = None
    app.state.workflow_validator_info = workflow_validator.get_validator_metadata()
    if app.state.workflow_validator_forward_url:
        app.state.workflow_validator = None
        yield
        return

    app.state.workflow_validator = workflow_validator.build_validator_bridge()
    try:
        await app.state.workflow_validator.start()
    except workflow_validator.WorkflowValidatorUnavailable as exc:
        app.state.workflow_validator_start_error = str(exc)
    try:
        yield
    finally:
        await app.state.workflow_validator.close()


def create_app() -> FastAPI:
    app = FastAPI(title="ops-proxy", lifespan=_lifespan)
    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

    async def _health_payload() -> dict:
        if app.state.workflow_validator_forward_url:
            forward_health = await _fetch_forward_health()
            payload = {
                "status": "ok",
                "validator_mode": "forward",
                "validator_forward_url": app.state.workflow_validator_forward_url,
            }
            if forward_health:
                payload["forward_status"] = forward_health.get("status")
                payload["forward_validator_mode"] = forward_health.get("validator_mode")
                payload["validator_info"] = forward_health.get("validator_info")
                if forward_health.get("status") not in {None, "ok"}:
                    payload["status"] = "degraded"
                if forward_health.get("detail"):
                    payload["forward_detail"] = forward_health.get("detail")
            return payload

        payload = {
            "status": "ok",
            "validator_mode": "local",
            "validator_info": app.state.workflow_validator_info,
        }
        if app.state.workflow_validator_start_error:
            payload["status"] = "degraded"
            payload["validator_start_error"] = app.state.workflow_validator_start_error
        return payload

    @app.get("/health")
    async def health():
        return await _health_payload()

    @app.get("/public/validator-health")
    async def public_validator_health():
        return await _health_payload()

    @app.get("/logs")
    @limiter.limit("30/minute")
    async def get_logs(
        request: Request,
        service: str,
        grep: str | None = None,
        lines: int = 500,
    ):
        admin_key = os.environ.get("LOGS_ADMIN_KEY", "")
        logs_dir = os.environ.get("LOGS_DIR", "/data/logs")
        if not admin_key:
            raise HTTPException(status_code=503, detail="Log access not configured")
        auth = request.headers.get("authorization") or ""
        if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], admin_key):
            raise HTTPException(status_code=401, detail="Invalid or missing admin key")
        if not _SERVICE_RE.match(service):
            raise HTTPException(status_code=400, detail="Invalid service name")
        if grep is not None and len(grep) > _MAX_GREP_LEN:
            raise HTTPException(status_code=400, detail="grep pattern too long")
        lines = max(0, min(lines, _MAX_LINES))
        try:
            pat = re.compile(grep) if grep else None
        except re.error:
            raise HTTPException(status_code=400, detail="Invalid grep pattern")
        path = os.path.join(logs_dir, f"{service}.log")
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="No such log")
        out = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if pat and not pat.search(line):
                    continue
                out.append(line)
        if lines > 0:
            out = out[-lines:]
        return PlainTextResponse("\n".join(out) + ("\n" if out else ""))

    @app.post("/public/validate-workflow")
    @limiter.limit(os.environ.get("WORKFLOW_VALIDATOR_RATE_LIMIT", "30/minute"))
    async def validate_workflow(request: Request):
        body = await request.body()
        if len(body) > _max_request_body_bytes():
            raise HTTPException(status_code=413, detail="Request body too large")

        if app.state.workflow_validator_forward_url:
            try:
                return await _forward_validation_request(body)
            except workflow_validator.WorkflowValidatorUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc))

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Malformed JSON body")

        try:
            request_data = workflow_validator.parse_validation_request(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        try:
            response = await workflow_validator.inspect_request_data(
                request_data,
                app.state.workflow_validator,
                validator_info=app.state.workflow_validator_info,
            )
            app.state.workflow_validator_start_error = None
            return response
        except workflow_validator.WorkflowValidatorUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    return app


app = create_app()
