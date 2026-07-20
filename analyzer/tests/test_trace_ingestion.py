from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from typer.testing import CliRunner

from atlas_analyzer.cli import app
from atlas_analyzer.ingestion import ingest_file, write_trace
from atlas_analyzer.query import load_map

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "atlas_claude_hook.py"
MAP_PATH = ROOT / "shared" / "fixtures" / "sample.map.json"
RUNNER = CliRunner()


def _run_hook(project: Path, payload: dict[str, object]) -> None:
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        check=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
    )


def test_claude_hook_pairs_phases_and_classifies_tests(tmp_path: Path) -> None:
    base = {
        "session_id": "session/one",
        "cwd": str(tmp_path),
        "tool_use_id": "tool-1",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "src" / "auth" / "session.py")},
    }
    _run_hook(tmp_path, {**base, "hook_event_name": "PreToolUse"})
    _run_hook(tmp_path, {**base, "hook_event_name": "PostToolUse"})
    _run_hook(
        tmp_path,
        {
            **base,
            "tool_use_id": "tool-2",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "hook_event_name": "PostToolUse",
        },
    )

    output = tmp_path / ".atlas" / "raw" / "session-one.jsonl"
    records = [json.loads(line) for line in output.read_text().splitlines()]

    assert len(records) == 3
    assert [record["turn"] for record in records] == [0, 0, 1]
    assert records[0]["path"] == "src/auth/session.py"
    assert records[2]["tool"] == "Test"
    assert records[1]["detail"]["success"] is True


def test_ingest_resolves_paths_deduplicates_and_keeps_provisional_nodes(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw.jsonl"
    records = [
        {
            "session_id": "cc-test",
            "agent": "claude-code",
            "timestamp": 100.0,
            "turn": 0,
            "tool": "Read",
            "path": "src/auth/session.py",
            "detail": {"hook_event": "PreToolUse", "tool_use_id": "read-1"},
        },
        {
            "session_id": "cc-test",
            "agent": "claude-code",
            "timestamp": 101.0,
            "turn": 0,
            "tool": "Read",
            "path": "src/auth/session.py",
            "detail": {
                "hook_event": "PostToolUse",
                "tool_use_id": "read-1",
                "success": True,
            },
        },
        {
            "session_id": "cc-test",
            "agent": "claude-code",
            "timestamp": 104.0,
            "turn": 1,
            "tool": "Write",
            "path": "src/auth/new_file.py",
            "detail": {"hook_event": "PostToolUse", "tool_use_id": "write-1"},
        },
        {
            "session_id": "cc-test",
            "agent": "claude-code",
            "timestamp": 106.0,
            "turn": 2,
            "tool": "Test",
            "path": "",
            "detail": {
                "hook_event": "PostToolUse",
                "tool_use_id": "test-1",
                "command": "pytest",
            },
        },
    ]
    raw.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    artifact = ingest_file(raw, load_map(MAP_PATH), repo_root=tmp_path)
    destination = tmp_path / "trace.json"
    write_trace(artifact, destination)

    assert [event.t for event in artifact.events] == [0.0, 3.0, 5.0]
    assert artifact.events[0].node_id == "file:src/auth/session.py"
    assert artifact.events[1].node_id == "file:src/auth/new_file.py"
    assert artifact.events[1].detail == {}
    assert artifact.summary.files_read == 1
    assert artifact.summary.files_edited == 1
    assert artifact.summary.tests_run == 1
    assert json.loads(destination.read_text())["map_ref"] == {
        "commit": "0123456789abcdef0123456789abcdef01234567"
    }


def test_ingest_cli_reports_trace_and_provisional_counts(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        json.dumps(
            {
                "session_id": "cli-trace",
                "agent": "claude-code",
                "timestamp": 1.0,
                "turn": 0,
                "tool": "Write",
                "path": "src/new.py",
                "detail": {
                    "hook_event": "PostToolUse",
                    "tool_use_id": "write-1",
                },
            }
        )
        + "\n"
    )
    output = tmp_path / "trace.json"

    result = RUNNER.invoke(
        app,
        [
            "ingest",
            str(raw),
            "--map",
            str(MAP_PATH),
            "--repo",
            str(tmp_path),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert "Ingested 1 events across 1 turns; 1 provisional paths" in result.stdout
    assert output.exists()
