#!/usr/bin/env python3
"""Append one Claude Code tool hook payload to an ATLAS raw JSONL session."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

TEST_COMMAND = re.compile(
    r"(^|[;&|]\s*|\s)"
    r"(pytest|vitest|jest|mocha|tox|nox|cargo\s+test|go\s+test|"
    r"npm\s+(run\s+)?test|pnpm\s+(run\s+)?test|yarn\s+test|make\s+test)"
    r"(\s|$)",
    re.IGNORECASE,
)
SUPPORTED_TOOLS = {"Read", "Edit", "Write", "Bash", "Grep"}
SECRET_PATTERNS = [
    # Authorization: Bearer <token>, Authorization: Basic <credentials>, ...
    (re.compile(r"(?i)(authorization\s*:\s*\S+\s+)\S+"), r"\1[REDACTED]"),
    # KEY=..., MY_TOKEN=..., PASSWORD=... assignments.
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*"
            r"\s*=\s*)(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1[REDACTED]",
    ),
    # --password foo, --token=foo, --api-key foo, -p style long flags.
    (
        re.compile(
            r"(?i)(--?(?:password|passwd|token|api-?key|secret)(?:[=\s]))"
            r"(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1[REDACTED]",
    ),
    # scheme://user:password@host credentials embedded in URLs.
    (re.compile(r"://([^/\s:@]+):([^@/\s]+)@"), r"://\1:[REDACTED]@"),
]


def _redacted_command(command: str) -> str:
    """Scrub common credential shapes before the command is persisted; raw
    trace logs are broadcast to viewer clients and must never carry secrets."""
    for pattern, replacement in SECRET_PATTERNS:
        command = pattern.sub(replacement, command)
    return command


def _safe_session_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return safe or "unknown-session"


def _project_root(payload: dict[str, Any]) -> Path:
    configured = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(configured or payload.get("cwd") or ".").resolve()


def _normalize_path(value: object, root: Path) -> str:
    if not isinstance(value, str) or not value:
        return ""
    candidate = Path(value)
    absolute = candidate if candidate.is_absolute() else root / candidate
    absolute = absolute.resolve(strict=False)
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError:
        return absolute.as_posix()


def _tool_and_path(
    payload: dict[str, Any],
    root: Path,
) -> tuple[str, str, dict[str, str | int | float | bool | None]]:
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    inputs = tool_input if isinstance(tool_input, dict) else {}
    detail: dict[str, str | int | float | bool | None] = {}
    if tool_name == "Bash":
        command = str(inputs.get("command") or "")
        detail["command"] = _redacted_command(command)
        tool = "Test" if TEST_COMMAND.search(command) else "Bash"
        return tool, "", detail
    if tool_name in {"Read", "Edit", "Write"}:
        return tool_name, _normalize_path(inputs.get("file_path"), root), detail
    if tool_name == "Grep":
        return "Grep", _normalize_path(inputs.get("path"), root), detail
    return tool_name, "", detail


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"next_turn": 0, "turns": {}}
    if not isinstance(value, dict):
        return {"next_turn": 0, "turns": {}}
    value.setdefault("next_turn", 0)
    value.setdefault("turns", {})
    return value


def capture(payload: dict[str, Any]) -> Path | None:
    tool_name = str(payload.get("tool_name") or "")
    if tool_name not in SUPPORTED_TOOLS:
        return None
    session_id = _safe_session_id(str(payload.get("session_id") or ""))
    root = _project_root(payload)
    raw_directory = root / ".atlas" / "raw"
    raw_directory.mkdir(parents=True, exist_ok=True)
    output = raw_directory / f"{session_id}.jsonl"
    state_path = raw_directory / f".{session_id}.state.json"
    lock_path = raw_directory / f".{session_id}.lock"
    tool_use_id = str(payload.get("tool_use_id") or "")
    hook_event = str(payload.get("hook_event_name") or "")
    tool, path, detail = _tool_and_path(payload, root)
    detail.update(
        {
            "hook_event": hook_event,
            "tool_use_id": tool_use_id,
        }
    )
    if hook_event == "PostToolUse":
        detail["success"] = True

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        state = _load_state(state_path)
        turns = state["turns"]
        # A missing tool_use_id must not collapse unrelated calls onto one
        # shared turn slot; give each such event its own key instead.
        turn_key = tool_use_id or f"_anonymous_{int(state['next_turn'])}"
        if turn_key not in turns:
            turns[turn_key] = int(state["next_turn"])
            state["next_turn"] = int(state["next_turn"]) + 1
        record = {
            "session_id": session_id,
            "agent": "claude-code",
            "timestamp": time.time(),
            "turn": int(turns[turn_key]),
            "tool": tool,
            "path": path,
            "detail": detail,
        }
        with output.open("a") as raw_file:
            raw_file.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            raw_file.flush()
            os.fsync(raw_file.fileno())
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
        temporary.replace(state_path)
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    return output


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")
        capture(payload)
    except Exception as error:  # Hooks must never block Claude's tool call.
        print(f"ATLAS trace hook: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
