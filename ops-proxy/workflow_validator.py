"""Workflow extraction, validation, and repair helpers for the server endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import deque
from pathlib import Path
from typing import Any

_MAX_STDERR_LINES = 20
_DEFAULT_MAX_ERRORS = 8
_MAX_REPAIR_MESSAGES = 50
_DEFAULT_VALIDATOR_TIMEOUT_SECONDS = 20


class WorkflowValidatorUnavailable(RuntimeError):
    """Raised when the persistent n8n-mcp validator process is unavailable."""


class NodeValidatorBridge:
    """Persistent stdio bridge to the Node-based n8n-mcp validator."""

    def __init__(
        self,
        script_path: str | Path | None = None,
        timeout_seconds: int = _DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
    ):
        self.script_path = Path(script_path or Path(__file__).with_name("validator_bridge.js"))
        self.timeout_seconds = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=_MAX_STDERR_LINES)
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        async with self._lock:
            if self._process and self._process.returncode is None:
                return

            self._stderr_tail.clear()
            self._process = await asyncio.create_subprocess_exec(
                "node",
                str(self.script_path),
                cwd=str(self.script_path.parent),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._stderr_task = asyncio.create_task(self._capture_stderr())

            ready_line = await self._read_stdout_line()
            if not ready_line:
                raise WorkflowValidatorUnavailable(
                    "Workflow validator failed to start"
                )
            try:
                ready = json.loads(ready_line)
            except json.JSONDecodeError as exc:
                raise WorkflowValidatorUnavailable(
                    f"Workflow validator emitted invalid startup payload: {ready_line}"
                ) from exc
            if not ready.get("ready"):
                detail = ready.get("error") or "Workflow validator failed to initialize"
                raise WorkflowValidatorUnavailable(detail)

    async def close(self) -> None:
        async with self._lock:
            if self._process and self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            self._process = None
            if self._stderr_task:
                self._stderr_task.cancel()
                self._stderr_task = None

    async def validate(self, workflow: dict[str, Any]) -> dict[str, Any]:
        if self._process is None or self._process.returncode is not None:
            await self.start()

        async with self._lock:
            if self._process is None or self._process.stdin is None:
                raise WorkflowValidatorUnavailable("Workflow validator is unavailable")
            if self._process.stdout is None:
                raise WorkflowValidatorUnavailable("Workflow validator stdout is unavailable")

            payload = json.dumps({"workflow": workflow}) + "\n"
            try:
                self._process.stdin.write(payload.encode("utf-8"))
                await asyncio.wait_for(
                    self._process.stdin.drain(),
                    timeout=self.timeout_seconds,
                )
                line = await self._read_stdout_line()
            except asyncio.TimeoutError as exc:
                raise WorkflowValidatorUnavailable(
                    "Workflow validator timed out"
                ) from exc
            except BrokenPipeError as exc:
                raise WorkflowValidatorUnavailable(
                    "Workflow validator process exited unexpectedly"
                ) from exc

            if not line:
                raise WorkflowValidatorUnavailable(self._failure_message())

            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkflowValidatorUnavailable(
                    f"Workflow validator returned invalid JSON: {line}"
                ) from exc

            if response.get("error"):
                raise WorkflowValidatorUnavailable(str(response["error"]))

            result = response.get("result")
            if not isinstance(result, dict):
                raise WorkflowValidatorUnavailable("Workflow validator returned no result payload")
            return result

    async def _read_stdout_line(self) -> str:
        if self._process is None or self._process.stdout is None:
            raise WorkflowValidatorUnavailable("Workflow validator stdout is unavailable")
        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise WorkflowValidatorUnavailable(
                "Workflow validator did not respond in time"
            ) from exc
        return line.decode("utf-8").strip()

    async def _capture_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", errors="replace").strip())

    def _failure_message(self) -> str:
        detail = " ".join(line for line in self._stderr_tail if line)
        if detail:
            return f"Workflow validator failed: {detail}"
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

    return inspection


def _normalize_message(message: str) -> str:
    return re.sub(r"\s+", " ", message).strip()
