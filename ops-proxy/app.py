"""Standalone admin service. GET /logs returns filtered slices of the durable
/data/logs/<service>.log files. Hardened: admin-key gated (constant-time compare),
rate-limited, input-capped, read-only; no coworker surface."""
import hmac
import os
import re
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import PlainTextResponse, JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

_SERVICE_RE = re.compile(r"^[a-z0-9-]+$")
_MAX_GREP_LEN = 200
_MAX_LINES = 10_000

app = FastAPI(title="ops-proxy")
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/logs")
@limiter.limit("30/minute")
async def get_logs(request: Request, service: str, grep: str | None = None,
                   lines: int = 500):
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
