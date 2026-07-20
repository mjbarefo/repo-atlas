from __future__ import annotations

import asyncio
import json
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from atlas_analyzer.query import load_map
from atlas_analyzer.watch import TraceWatcher

ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = ROOT / "shared" / "fixtures" / "sample.map.json"


def _raw_event(
    *,
    timestamp: float,
    tool_use_id: str,
    tool: str,
    path: str,
    turn: int,
) -> dict[str, object]:
    return {
        "session_id": "live-test",
        "agent": "claude-code",
        "timestamp": timestamp,
        "turn": turn,
        "tool": tool,
        "path": path,
        "detail": {
            "hook_event": "PostToolUse",
            "tool_use_id": tool_use_id,
        },
    }


def test_watcher_sends_initial_and_tailed_snapshots(tmp_path: Path) -> None:
    async def scenario() -> None:
        raw = tmp_path / "session.jsonl"
        raw.write_text(
            json.dumps(
                _raw_event(
                    timestamp=10.0,
                    tool_use_id="read-1",
                    tool="Read",
                    path="src/auth/session.py",
                    turn=0,
                )
            )
            + "\n"
        )
        watcher = TraceWatcher(
            raw,
            load_map(MAP_PATH),
            repo_root=tmp_path,
            poll_interval=0.01,
        )
        async with serve(watcher.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            tail_task = asyncio.create_task(watcher.tail())
            try:
                async with connect(
                    f"ws://127.0.0.1:{port}",
                    proxy=None,
                ) as websocket:
                    initial = json.loads(await asyncio.wait_for(websocket.recv(), 1))
                    assert len(initial["trace"]["events"]) == 1

                    with raw.open("a") as raw_file:
                        raw_file.write(
                            json.dumps(
                                _raw_event(
                                    timestamp=12.0,
                                    tool_use_id="edit-1",
                                    tool="Edit",
                                    path="src/auth/session.py",
                                    turn=1,
                                )
                            )
                            + "\n"
                        )
                    updated = json.loads(await asyncio.wait_for(websocket.recv(), 1))
                    assert [event["tool"] for event in updated["trace"]["events"]] == [
                        "Read",
                        "Edit",
                    ]
            finally:
                tail_task.cancel()
                try:
                    await tail_task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())
