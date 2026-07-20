"""Tail raw trace JSONL and publish resolved snapshots over WebSocket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from .ingestion import ingest_file
from .models import MapArtifact


class TraceWatcher:
    def __init__(
        self,
        raw_path: Path,
        artifact: MapArtifact,
        *,
        repo_root: Path,
        poll_interval: float = 0.15,
    ) -> None:
        self.raw_path = raw_path
        self.artifact = artifact
        self.repo_root = repo_root
        self.poll_interval = poll_interval
        self.clients: set[ServerConnection] = set()
        self.offset = raw_path.stat().st_size

    def snapshot_message(self) -> str:
        trace = ingest_file(
            self.raw_path,
            self.artifact,
            repo_root=self.repo_root,
        )
        return json.dumps(
            {
                "type": "snapshot",
                "trace": trace.model_dump(mode="json"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    async def handler(self, connection: ServerConnection) -> None:
        self.clients.add(connection)
        try:
            await connection.send(self.snapshot_message())
            await connection.wait_closed()
        finally:
            self.clients.discard(connection)

    async def broadcast_snapshot(self) -> None:
        if not self.clients:
            return
        message = self.snapshot_message()
        await asyncio.gather(
            *(client.send(message) for client in tuple(self.clients)),
            return_exceptions=True,
        )

    async def tail(self) -> None:
        while True:
            size = self.raw_path.stat().st_size
            if size < self.offset:
                self.offset = 0
            changed = False
            if size > self.offset:
                with self.raw_path.open() as raw_file:
                    raw_file.seek(self.offset)
                    while True:
                        start = raw_file.tell()
                        line = raw_file.readline()
                        if not line:
                            break
                        if not line.endswith("\n"):
                            self.offset = start
                            break
                        self.offset = raw_file.tell()
                        if line.strip():
                            try:
                                value: Any = json.loads(line)
                            except ValueError:
                                continue
                            changed = changed or isinstance(value, dict)
            if changed:
                await self.broadcast_snapshot()
            await asyncio.sleep(self.poll_interval)


async def watch_trace(
    raw_path: Path,
    artifact: MapArtifact,
    *,
    repo_root: Path,
    host: str,
    port: int,
) -> None:
    watcher = TraceWatcher(raw_path, artifact, repo_root=repo_root)
    async with serve(watcher.handler, host, port):
        await watcher.tail()
