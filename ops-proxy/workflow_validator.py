"""Workflow extraction, validation, and repair helpers for the server endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

_DEFAULT_MAX_ERRORS = 8
_MAX_REPAIR_MESSAGES = 50
_DEFAULT_VALIDATOR_TIMEOUT_SECONDS = 20


class WorkflowValidatorUnavailable(RuntimeError):
    """Raised when the Node-based n8n-mcp validator process is unavailable."""


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_n8n_mcp_install_root() -> Path | None:
    env_root = os.environ.get("N8N_MCP_INSTALL_ROOT", "").strip()
    candidates = []
    if env_root:
        candidates.append(Path(env_root))

    script_dir = _script_dir()
    candidates.extend(
        [
            script_dir / "node_modules" / "n8n-mcp",
            script_dir.parent / "node_modules" / "n8n-mcp",
        ]
    )

    for candidate in candidates:
        if (candidate / "package.json").is_file():
            return candidate
    return None


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _nodes_content_sha256(path: Path) -> str | None:
    """Stable content hash of the nodes table.

    The physical nodes.db file mutates during normal n8n-mcp use (SQLite change
    counter, freed pages, FTS internals) without the node data changing, so a
    whole-file hash produces false mismatches between a fresh install and a
    used one. Hashing the ordered rows of the nodes table compares the data
    that actually drives validation. Must stay byte-identical with
    n8n-knowledge hooks/lib/validator_metadata.py:_nodes_content_sha256.
    """
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        digest = hashlib.sha256()
        for row in db.execute("SELECT * FROM nodes ORDER BY node_type"):
            digest.update(repr(row).encode("utf-8"))
        return digest.hexdigest()
    except sqlite3.Error:
        return None
    finally:
        db.close()


def get_validator_metadata() -> dict[str, Any]:
    script_dir = _script_dir()
    configured_package = _read_json_file(script_dir / "package.json") or {}
    install_root = _resolve_n8n_mcp_install_root()
    installed_package = (
        _read_json_file(install_root / "package.json") if install_root is not None else None
    ) or {}
    nodes_db_path = install_root / "data" / "nodes.db" if install_root is not None else None
    nodes_db_exists = nodes_db_path is not None and nodes_db_path.is_file()

    return {
        "validator_engine": "n8n-mcp",
        "configured_n8n_mcp_version": (
            configured_package.get("dependencies", {}) or {}
        ).get("n8n-mcp"),
        "installed_n8n_mcp_version": installed_package.get("version"),
        "nodes_db_sha256": _sha256_file(nodes_db_path) if nodes_db_exists else None,
        "nodes_content_sha256": (
            _nodes_content_sha256(nodes_db_path) if nodes_db_exists else None
        ),
    }


class NodeValidatorBridge:
    """One-shot stdio bridge to the Node-based n8n-mcp validator."""

    def __init__(
        self,
        script_path: str | Path | None = None,
        timeout_seconds: int = _DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
    ):
        self.script_path = Path(script_path or Path(__file__).with_name("validator_bridge.js"))
        self.timeout_seconds = timeout_seconds
        self.n8n_mcp_install_root = os.environ.get("N8N_MCP_INSTALL_ROOT", "").strip()

    async def start(self) -> None:
        if not self.script_path.is_file():
            raise WorkflowValidatorUnavailable(
                f"Workflow validator bridge script is missing: {self.script_path}"
            )

    async def close(self) -> None:
        return None

    async def validate(self, workflow: dict[str, Any]) -> dict[str, Any]:
        await self.start()

        env = dict(os.environ)
        if self.n8n_mcp_install_root:
            env["N8N_MCP_INSTALL_ROOT"] = self.n8n_mcp_install_root

        try:
            process = await asyncio.create_subprocess_exec(
                "node",
                str(self.script_path),
                cwd=str(self.script_path.parent),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise WorkflowValidatorUnavailable(
                "Node.js is not installed for the workflow validator"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(workflow).encode("utf-8")),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise WorkflowValidatorUnavailable("Workflow validator timed out") from exc

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            raise WorkflowValidatorUnavailable(
                self._failure_message(stderr_text, stdout_text)
            )
        if not stdout_text:
            raise WorkflowValidatorUnavailable(
                self._failure_message(stderr_text, stdout_text)
            )

        try:
            response = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise WorkflowValidatorUnavailable(
                f"Workflow validator returned invalid JSON: {stdout_text}"
            ) from exc

        if not isinstance(response, dict):
            raise WorkflowValidatorUnavailable("Workflow validator returned no result payload")
        return response

    def _failure_message(self, stderr_text: str, stdout_text: str) -> str:
        detail = " ".join(part for part in [stderr_text, stdout_text] if part)
        if detail:
            return f"Workflow validator failed: {detail[:1000]}"
        return "Workflow validator failed without details"


def build_validator_bridge() -> NodeValidatorBridge:
    return NodeValidatorBridge()


def extract_workflow_json(response_text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Extract n8n workflow JSON from a model response."""
    json_blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", response_text, re.DOTALL)

    for block in json_blocks:
        try:
            obj = json.loads(block.strip())
            if isinstance(obj, dict) and ("nodes" in obj or "connections" in obj):
                return obj, None
        except json.JSONDecodeError:
            continue

    brace_starts = [match.start() for match in re.finditer(r"\{", response_text)]
    for start in brace_starts:
        try:
            candidate = response_text[start:]
            depth = 0
            end = 0
            for index, char in enumerate(candidate):
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end = index + 1
                        break
            snippet = candidate[:end]
            if len(snippet) > 200:
                obj = json.loads(snippet)
                if isinstance(obj, dict) and ("nodes" in obj or "connections" in obj):
                    return obj, None
        except (json.JSONDecodeError, ValueError):
            continue

    return None, "no_json_found"


def summarize_validation(validation: dict[str, Any], max_errors: int = _DEFAULT_MAX_ERRORS) -> list[str]:
    """Return a short deduped list of actionable validator issues."""
    seen = set()
    summary = []
    for err in validation.get("errors", []):
        message = _normalize_message(err.get("message", "unknown validator error"))
        if not message or message in seen:
            continue
        seen.add(message)
        summary.append(message)
        if len(summary) >= max_errors:
            break
    return summary


def parse_validation_request(payload: Any) -> dict[str, Any]:
    """Normalize accepted request shapes into one internal contract."""
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")

    if ("nodes" in payload or "connections" in payload) and "workflow" not in payload:
        workflow = payload
        response_text = None
        original_prompt = None
        include_repair_prompt = False
        debug = False
        max_errors = _DEFAULT_MAX_ERRORS
    else:
        workflow = payload.get("workflow")
        response_text = payload.get("response_text")
        original_prompt = payload.get("original_prompt")
        include_repair_prompt = payload.get("include_repair_prompt", False)
        debug = payload.get("debug", False)
        max_errors = payload.get("max_errors", _DEFAULT_MAX_ERRORS)

    if workflow is not None and not isinstance(workflow, dict):
        raise ValueError("'workflow' must be a JSON object")
    if response_text is not None and not isinstance(response_text, str):
        raise ValueError("'response_text' must be a string")
    if original_prompt is not None and not isinstance(original_prompt, str):
        raise ValueError("'original_prompt' must be a string")
    if not isinstance(include_repair_prompt, bool):
        raise ValueError("'include_repair_prompt' must be a boolean")
    if not isinstance(debug, bool):
        raise ValueError("'debug' must be a boolean")
    if not isinstance(max_errors, int):
        raise ValueError("'max_errors' must be an integer")
    if not 1 <= max_errors <= _MAX_REPAIR_MESSAGES:
        raise ValueError("'max_errors' must be between 1 and 50")
    if workflow is None and not response_text:
        raise ValueError("Provide either 'workflow' or 'response_text'")

    return {
        "workflow": workflow,
        "response_text": response_text,
        "original_prompt": original_prompt,
        "include_repair_prompt": include_repair_prompt,
        "debug": debug,
        "max_errors": max_errors,
    }


async def inspect_request_data(
    request_data: dict[str, Any],
    validator: NodeValidatorBridge,
    validator_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the server-side validation response contract."""
    workflow = request_data["workflow"]
    response_text = request_data["response_text"] or ""

    if workflow is None:
        workflow, extract_error = extract_workflow_json(response_text)
    else:
        extract_error = None

    if workflow is None:
        repair_messages = [
            "Return a single complete importable n8n workflow JSON object inside a ```json code block.",
            "Include both a 'nodes' array and a 'connections' object.",
        ]
        inspection = {
            "valid": False,
            "has_json": False,
            "extract_error": extract_error,
            "error_count": 1,
            "warning_count": 0,
            "node_count": 0,
            "trigger_count": 0,
            "repair_messages": repair_messages,
            "feedback_block": "\n".join(f"- {message}" for message in repair_messages),
            "errors": [{"type": "extract_error", "message": extract_error or "no_json_found"}],
            "warnings": [],
            "statistics": {"totalNodes": 0, "triggerNodes": 0},
            "suggestions": [],
            "workflow": None,
        }
    else:
        validation = await validator.validate(workflow)
        statistics = validation.get("statistics") or {}
        repair_messages = summarize_validation(
            validation,
            max_errors=request_data["max_errors"],
        )
        inspection = {
            "valid": bool(validation.get("valid")),
            "has_json": True,
            "extract_error": None,
            "error_count": int(validation.get("error_count", len(validation.get("errors", [])))),
            "warning_count": int(
                validation.get("warning_count", len(validation.get("warnings", [])))
            ),
            "node_count": int(statistics.get("totalNodes", len(workflow.get("nodes", [])))),
            "trigger_count": int(statistics.get("triggerNodes", 0)),
            "repair_messages": repair_messages,
            "feedback_block": "\n".join(f"- {message}" for message in repair_messages),
            "errors": validation.get("errors", []),
            "warnings": validation.get("warnings", []),
            "statistics": statistics,
            "suggestions": validation.get("suggestions", []),
            "workflow": workflow,
        }

    if not request_data["debug"]:
        inspection.pop("workflow", None)

    if validator_info is not None:
        inspection["validator_info"] = validator_info

    return inspection


def _normalize_message(message: str) -> str:
    return re.sub(r"\s+", " ", message).strip()
