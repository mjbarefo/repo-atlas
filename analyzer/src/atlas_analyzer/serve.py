"""Local HTTP server for the ATLAS viewer and one selected map artifact."""

from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import ImpactArtifact, TraceArtifact
from .query import load_map

_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


class AtlasViewerServer(ThreadingHTTPServer):
    map_payload: bytes
    trace_payload: bytes | None
    impact_payload: bytes | None
    context_payload: bytes
    viewer_directory: Path
    # Hostnames a request's Host header may name, or None to disable the
    # check when the operator explicitly binds a non-loopback interface.
    allowed_hostnames: frozenset[str] | None


class AtlasViewerHandler(SimpleHTTPRequestHandler):
    """Serve the built viewer and expose its map through a same-origin endpoint."""

    server: AtlasViewerServer

    def __init__(
        self,
        request: Any,
        client_address: Any,
        server: AtlasViewerServer,
    ) -> None:
        super().__init__(
            request,
            client_address,
            server,
            directory=str(server.viewer_directory),
        )

    def _host_rejected(self) -> bool:
        """Reject requests whose Host header does not name this loopback
        server. A loopback bind alone does not stop DNS-rebinding: a hostile
        page can re-resolve its own hostname to 127.0.0.1 and read the API
        as if it were same-origin unless the Host header is validated."""
        allowed = self.server.allowed_hostnames
        if allowed is None:
            return False
        try:
            hostname = urlsplit(f"//{self.headers.get('Host', '')}").hostname
        except ValueError:
            hostname = None
        if hostname in allowed:
            return False
        self.send_error(403, "Host header not allowed")
        return True

    def do_HEAD(self) -> None:  # noqa: N802 - inherited HTTP method name
        if self._host_rejected():
            return
        super().do_HEAD()

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP method name
        if self._host_rejected():
            return
        if self.path == "/api/map":
            self._json(self.server.map_payload)
            return
        if self.path == "/api/health":
            self._json(b'{"status":"ok"}')
            return
        if self.path == "/api/context":
            self._json(self.server.context_payload)
            return
        if self.path == "/api/trace":
            if self.server.trace_payload is None:
                self.send_error(404, "No trace selected")
            else:
                self._json(self.server.trace_payload)
            return
        if self.path == "/api/impact":
            if self.server.impact_payload is None:
                self.send_error(404, "No impact selected")
            else:
                self._json(self.server.impact_payload)
            return
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def find_viewer_directory(explicit: Path | None = None) -> Path:
    candidates = [explicit] if explicit is not None else []
    candidates.extend(
        [
            Path.cwd() / "viewer" / "dist",
            Path(__file__).resolve().parents[3] / "viewer" / "dist",
        ]
    )
    for candidate in candidates:
        if candidate is not None and (candidate / "index.html").is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "viewer build not found; run `npm --prefix viewer run build` "
        "or pass --viewer-dist"
    )


def create_server(
    map_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 4173,
    viewer_directory: Path | None = None,
    repo_root: Path | None = None,
    trace_path: Path | None = None,
    impact_path: Path | None = None,
    watch_url: str = "ws://127.0.0.1:8765",
) -> AtlasViewerServer:
    artifact = load_map(map_path)
    server = AtlasViewerServer((host, port), AtlasViewerHandler)
    server.allowed_hostnames = (
        _LOOPBACK_HOSTNAMES if host in _LOOPBACK_HOSTNAMES else None
    )
    server.map_payload = json.dumps(
        artifact.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    resolved_root = repo_root or (
        map_path.parent.parent if map_path.parent.name == ".atlas" else map_path.parent
    )
    server.context_payload = json.dumps(
        {
            "repo_root": str(resolved_root.resolve()),
            "watch_url": watch_url,
        },
        separators=(",", ":"),
    ).encode()
    if trace_path is None:
        server.trace_payload = None
    else:
        trace = TraceArtifact.model_validate(json.loads(trace_path.read_text()))
        server.trace_payload = json.dumps(
            trace.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    if impact_path is None:
        server.impact_payload = None
    else:
        impact = ImpactArtifact.model_validate(json.loads(impact_path.read_text()))
        if impact.map_ref.commit != artifact.repo.commit:
            raise ValueError(
                f"impact map {impact.map_ref.commit} does not match "
                f"selected map {artifact.repo.commit}"
            )
        server.impact_payload = json.dumps(
            impact.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    server.viewer_directory = find_viewer_directory(viewer_directory)
    return server
