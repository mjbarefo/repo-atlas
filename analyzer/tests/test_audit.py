from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from atlas_analyzer.audit import attach_audit, audit_map, explain_node
from atlas_analyzer.cli import app
from atlas_analyzer.models import MapArtifact

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_MAP = ROOT / "shared" / "fixtures" / "sample.map.json"
RUNNER = CliRunner()


def _audit_fixture() -> MapArtifact:
    payload: dict[str, Any] = json.loads(SAMPLE_MAP.read_text())
    nodes = {node["id"]: node for node in payload["nodes"]}
    nodes["file:src/auth/session.py"]["metrics"]["loc"] = 900
    nodes["mod:auth.core"]["metrics"]["loc"] = 2600
    nodes["comp:auth"]["children"].append("mod:build")
    payload["nodes"].extend(
        [
            {
                "id": "mod:build",
                "kind": "module",
                "role": "source",
                "label": "Build Config",
                "summary": "Build configuration.",
                "prose_source": "heuristic",
                "children": ["file:vite.config.ts"],
                "files": [],
                "metrics": {"loc": 7, "fan_in": 1, "fan_out": 1},
            },
            {
                "id": "file:vite.config.ts",
                "kind": "file",
                "role": "source",
                "label": "vite.config.ts",
                "summary": "Build configuration.",
                "prose_source": "heuristic",
                "children": [],
                "files": ["vite.config.ts"],
                "metrics": {"loc": 7, "fan_in": 0, "fan_out": 0},
            },
        ]
    )
    payload["edges"].extend(
        [
            {
                "source": "mod:auth.core",
                "target": "mod:build",
                "kind": "imports",
                "evidence": [{"file": "src/auth/session.py", "line": 5}],
                "weight": 1,
            },
            {
                "source": "mod:build",
                "target": "mod:auth.core",
                "kind": "imports",
                "evidence": [{"file": "vite.config.ts", "line": 1}],
                "weight": 1,
            },
        ]
    )
    payload["levels"]["component"]["comp:auth"].append("mod:build")
    payload["levels"]["module"]["mod:build"] = ["file:vite.config.ts"]
    return MapArtifact.model_validate(payload)


def test_audit_reports_evidence_backed_findings_deterministically() -> None:
    report = audit_map(_audit_fixture())

    assert report.summary.warnings == 3
    assert report.summary.notices == 1
    assert [finding.code for finding in report.findings] == [
        "large-file",
        "large-module",
        "module-cycle",
        "thin-module",
    ]
    assert report.findings[0].node_ids[0].root == "file:src/auth/session.py"
    assert [item.root for item in report.findings[2].node_ids] == [
        "mod:auth.core",
        "mod:build",
    ]
    assert [item.root for item in report.findings[2].evidence] == [
        "Auth Core -> Build Config: 1 imports site(s)",
        "Build Config -> Auth Core: 1 imports site(s)",
    ]
    assert report.model_dump_json() == audit_map(_audit_fixture()).model_dump_json()


def test_explain_node_includes_hierarchy_relationships_and_audit() -> None:
    artifact = attach_audit(_audit_fixture())

    explanation = explain_node(artifact, "file:src/auth/session.py")

    assert [item["id"] for item in explanation["hierarchy"]] == [
        "comp:auth",
        "mod:auth.core",
    ]
    assert explanation["relationships"][0]["direction"] == "depends_on"
    assert explanation["relationships"][0]["node"]["id"] == "file:src/auth/store.py"
    assert [finding["code"] for finding in explanation["audit_findings"]] == [
        "large-file"
    ]


def test_audit_and_explain_cli_support_text_and_json(tmp_path: Path) -> None:
    map_path = tmp_path / "map.json"
    map_path.write_text(attach_audit(_audit_fixture()).model_dump_json(indent=2) + "\n")

    audit_text = RUNNER.invoke(app, ["audit", "--map", str(map_path)])
    audit_json = RUNNER.invoke(
        app, ["audit", "--map", str(map_path), "--format", "json"]
    )
    explain_text = RUNNER.invoke(
        app,
        [
            "explain",
            "file:src/auth/session.py",
            "--map",
            str(map_path),
        ],
    )
    explain_json = RUNNER.invoke(
        app,
        [
            "explain",
            "file:src/auth/session.py",
            "--map",
            str(map_path),
            "--format",
            "json",
        ],
    )

    assert audit_text.exit_code == 0
    assert "3 warning(s), 1 notice(s)" in audit_text.stdout
    assert json.loads(audit_json.stdout)["summary"]["warnings"] == 3
    assert explain_text.exit_code == 0
    assert "Hierarchy: Authentication > Auth Core > session.py" in explain_text.stdout
    assert "depends on store.py" in explain_text.stdout
    assert json.loads(explain_json.stdout)["node"]["id"] == "file:src/auth/session.py"
