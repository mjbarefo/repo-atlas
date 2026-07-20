from __future__ import annotations

import json
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

from atlas_analyzer.serve import create_server


ROOT = Path(__file__).resolve().parents[2]


def test_server_exposes_selected_map_and_viewer(tmp_path: Path) -> None:
    viewer = tmp_path / "viewer"
    viewer.mkdir()
    (viewer / "index.html").write_text("<h1>ATLAS</h1>")
    map_path = ROOT / "shared" / "fixtures" / "sample.map.json"
    trace_path = ROOT / "shared" / "fixtures" / "sample.trace.json"
    impact_path = ROOT / "shared" / "fixtures" / "sample.impact.json"
    server = create_server(
        map_path,
        port=0,
        viewer_directory=viewer,
        repo_root=tmp_path,
        trace_path=trace_path,
        impact_path=impact_path,
    )
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        host, port = server.server_address[:2]
        with urlopen(f"http://{host}:{port}/api/map") as response:
            payload = json.load(response)
            assert response.headers["Content-Type"].startswith("application/json")
        with urlopen(f"http://{host}:{port}/") as response:
            assert response.read() == b"<h1>ATLAS</h1>"
        with urlopen(f"http://{host}:{port}/api/context") as response:
            context = json.load(response)
        with urlopen(f"http://{host}:{port}/api/trace") as response:
            trace = json.load(response)
        with urlopen(f"http://{host}:{port}/api/impact") as response:
            impact = json.load(response)
        assert payload["schema_version"] == "1.0"
        assert payload["nodes"][0]["id"] == "comp:auth"
        assert context == {
            "repo_root": str(tmp_path),
            "watch_url": "ws://127.0.0.1:8765",
        }
        assert trace["session_id"] == "cc-2026-07-20-a"
        assert impact["summary"]["changed_files"] == 2
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
