"""Standalone admin + workflow validation service."""
from contextlib import asynccontextmanager
from functools import wraps
import hmac
import json
import os
import re

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse, PlainTextResponse

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

limiter = Limiter(key_func=get_remote_address)


def _max_request_body_bytes() -> int:
    raw = os.environ.get("WORKFLOW_VALIDATOR_MAX_BODY_BYTES", "")
    try:
        value = int(raw) if raw else _DEFAULT_MAX_REQUEST_BODY_BYTES
    except ValueError:
        value = _DEFAULT_MAX_REQUEST_BODY_BYTES
    return max(1_024, value)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.workflow_validator = workflow_validator.build_validator_bridge()
    app.state.workflow_validator_start_error = None
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

    @app.get("/health")
    async def health():
        return {"status": "ok"}

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
    @limiter.limit("30/minute")
    async def validate_workflow(request: Request):
        body = await request.body()
        if len(body) > _max_request_body_bytes():
            raise HTTPException(status_code=413, detail="Request body too large")

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
            )
            app.state.workflow_validator_start_error = None
            return response
        except workflow_validator.WorkflowValidatorUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    return app


app = create_app()
