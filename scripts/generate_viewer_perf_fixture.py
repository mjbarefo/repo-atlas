"""Generate a deterministic 500-file map for the Phase 3 browser gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def fixture() -> dict[str, Any]:
    component_id = "comp:performance-gate"
    module_id = "mod:performance-gate"
    file_ids = [f"file:src/file_{index:03}.py" for index in range(500)]
    file_nodes = [
        {
            "id": node_id,
            "kind": "file",
            "label": f"file_{index:03}.py",
            "summary": "Synthetic source file for the Phase 3 rendering gate.",
            "prose_source": "heuristic",
            "children": [],
            "files": [f"src/file_{index:03}.py"],
            "metrics": {
                "loc": 1,
                "fan_in": 0 if index == 0 else 1,
                "fan_out": 0 if index == 499 else 1,
            },
        }
        for index, node_id in enumerate(file_ids)
    ]
    edges = [
        {
            "source": file_ids[index],
            "target": file_ids[index + 1],
            "kind": "imports",
            "evidence": [{"file": f"src/file_{index:03}.py", "line": 1}],
            "label": f"file_{index + 1:03}",
        }
        for index in range(499)
    ]
    return {
        "schema_version": "1.0",
        "repo": {
            "root": ".",
            "commit": "0000000000000000000000000000000000000000",
            "generated_at": "2026-07-20T00:00:00Z",
        },
        "nodes": [
            {
                "id": component_id,
                "kind": "component",
                "label": "Performance Gate",
                "summary": "One 500-file module for browser rendering verification.",
                "prose_source": "heuristic",
                "children": [module_id],
                "files": [],
                "metrics": {"loc": 500, "fan_in": 0, "fan_out": 0},
            },
            {
                "id": module_id,
                "kind": "module",
                "label": "500 File Level",
                "summary": "A dense level that activates the Canvas edge fallback.",
                "prose_source": "heuristic",
                "children": file_ids,
                "files": [],
                "metrics": {"loc": 500, "fan_in": 0, "fan_out": 0},
            },
            *file_nodes,
        ],
        "edges": edges,
        "levels": {
            "system": [component_id],
            "component": {component_id: [module_id]},
            "module": {module_id: file_ids},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
