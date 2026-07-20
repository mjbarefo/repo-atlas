"""Tail raw trace JSONL and publish resolved snapshots over WebSocket."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from .ingestion import ingest_file
from .models import MapArtifact

_LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def _origin_allowed(origin: str | None) -> bool:
    """Reject browser cross-origin connections; loopback pages and non-browser
    clients (which send no Origin header) are allowed. Browsers do not apply
    the same-origin policy to WebSocket connections, so without this check any
    website open in a local browser could read the live trace."""
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in _LOOPBACK_HOSTNAMES


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

    def snapshot_message(self) -> str | None:
        """Return the current snapshot, or None when the raw log is empty or
        contains records the ingester rejects; one bad line must not kill the
        long-running watcher or every future client connection."""
        try:
            trace = ingest_file(
                self.raw_path,
                self.artifact,
                repo_root=self.repo_root,
            )
        except (OSError, ValueError) as error:
            print(f"atlas watch: snapshot skipped: {error}", file=sys.stderr)
            return None
        return json.dumps(
            {
                "type": "snapshot",
                "trace": trace.model_dump(mode="json"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        if not _origin_allowed(request.headers.get("Origin")):
            return connection.respond(HTTPStatus.FORBIDDEN, "origin not allowed\n")
        return None

    async def handler(self, connection: ServerConnection) -> None:
        self.clients.add(connection)
        try:
            message = self.snapshot_message()
            if message is not None:
                await connection.send(message)
            await connection.wait_closed()
        finally:
            self.clients.discard(connection)

    async def broadcast_snapshot(self) -> None:
        if not self.clients:
            return
        message = self.snapshot_message()
        if message is None:
            return
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
    async with serve(
        watcher.handler, host, port, process_request=watcher.process_request
    ):
        await watcher.tail()
