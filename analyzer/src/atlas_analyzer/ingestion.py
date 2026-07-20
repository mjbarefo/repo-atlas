"""Resolve raw Claude Code hook events against an ATLAS map artifact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifact_io import atomic_write_text
from .models import MapArtifact, TraceArtifact


def _event_timestamp(event: dict[str, Any]) -> float:
    try:
        return float(event.get("timestamp", 0))
    except (TypeError, ValueError):
        return 0.0


def _event_turn(event: dict[str, Any]) -> int:
    try:
        return int(event.get("turn", 0))
    except (TypeError, ValueError):
        return 0


def _normalized_path(value: str, repo_root: Path) -> str:
    if not value:
        return ""
    candidate = Path(value)
    absolute = candidate if candidate.is_absolute() else repo_root / candidate
    absolute = absolute.resolve(strict=False)
    try:
        return absolute.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return absolute.as_posix()


def load_raw_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as error:
            raise ValueError(f"invalid JSON on line {line_number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"raw event on line {line_number} is not an object")
        value["_sequence"] = len(events)
        events.append(value)
    if not events:
        raise ValueError("raw trace contains no events")
    return events


def _deduplicated(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for event in events:
        detail = event.get("detail")
        details = detail if isinstance(detail, dict) else {}
        tool_use_id = details.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            anonymous.append(event)
            continue
        current = selected.get(tool_use_id)
        hook_event = details.get("hook_event")
        if current is None or hook_event == "PostToolUse":
            selected[tool_use_id] = event
    return sorted(
        [*selected.values(), *anonymous],
        key=lambda item: (
            _event_timestamp(item),
            int(item.get("_sequence", 0)),
        ),
    )


def _file_index(artifact: MapArtifact) -> dict[str, str]:
    index: dict[str, str] = {}
    for node in artifact.nodes:
        if node.kind.value != "file":
            continue
        for path in node.files:
            index[path.root.replace("\\", "/").removeprefix("./")] = node.id
        if node.id.startswith("file:"):
            index.setdefault(node.id.removeprefix("file:"), node.id)
    return index


def ingest_events(
    raw_events: list[dict[str, Any]],
    artifact: MapArtifact,
    *,
    repo_root: Path,
) -> TraceArtifact:
    events = _deduplicated(raw_events)
    file_index = _file_index(artifact)
    first_timestamp = min(_event_timestamp(event) for event in events)
    resolved: list[dict[str, Any]] = []
    for event in events:
        path = _normalized_path(str(event.get("path") or ""), repo_root)
        node_id = file_index.get(path)
        if path and node_id is None:
            node_id = f"file:{path}"
        detail = event.get("detail")
        details = dict(detail) if isinstance(detail, dict) else {}
        details.pop("hook_event", None)
        details.pop("tool_use_id", None)
        resolved.append(
            {
                "t": max(0.0, _event_timestamp(event) - first_timestamp),
                "tool": str(event.get("tool") or ""),
                "path": path,
                "node_id": node_id,
                "detail": details,
                "turn": max(0, _event_turn(event)),
            }
        )

    read_paths = {
        event["path"] for event in resolved if event["tool"] == "Read" and event["path"]
    }
    edited_paths = {
        event["path"]
        for event in resolved
        if event["tool"] in {"Edit", "Write"} and event["path"]
    }
    session_id = str(
        events[0].get("session_id") or raw_events[0].get("session_id") or ""
    )
    agent = str(events[0].get("agent") or "claude-code")
    return TraceArtifact.model_validate(
        {
            "schema_version": "1.0",
            "session_id": session_id,
            "agent": agent,
            "map_ref": {"commit": artifact.repo.commit},
            "events": resolved,
            "summary": {
                "files_read": len(read_paths),
                "files_edited": len(edited_paths),
                "tests_run": sum(event["tool"] == "Test" for event in resolved),
            },
        }
    )


def ingest_file(
    raw_path: Path,
    artifact: MapArtifact,
    *,
    repo_root: Path,
) -> TraceArtifact:
    return ingest_events(load_raw_events(raw_path), artifact, repo_root=repo_root)


def write_trace(artifact: TraceArtifact, destination: Path) -> None:
    payload = json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    atomic_write_text(destination, payload + "\n")
