import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest
from typer.testing import CliRunner

from atlas_analyzer.analysis import analyzer
from atlas_analyzer.analysis.analyzer import (
    analyze_repository,
    analyze_repository_incremental,
    write_map,
)
from atlas_analyzer.cli import app
from atlas_analyzer.config import load_config, provider_environment
from atlas_analyzer.enrichment import ProviderResult
from atlas_analyzer.enrichment.contracts import (
    ClusterEnrichment,
    SystemEnrichment,
)

FIXTURE = Path(__file__).parent / "fixtures" / "golden_repo"
RUNNER = CliRunner()


def _git_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "atlas@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "ATLAS Tests"],
        check=True,
    )
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "initial"], check=True)
    return repo


def test_five_file_incremental_run_parses_only_changes_and_matches_full(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _git_fixture(tmp_path)
    baseline = analyze_repository(repo)
    changed = [
        "src/app.py",
        "src/auth/__init__.py",
        "src/auth/session.py",
        "web/lib/client.ts",
        "web/main.ts",
    ]
    for relative in changed:
        path = repo / relative
        comment = (
            "# incremental fixture"
            if path.suffix == ".py"
            else "// incremental fixture"
        )
        path.write_text(path.read_text() + f"\n{comment}\n")

    parsed: list[Path] = []
    original = analyzer.parse_file

    def recording_parse(path: Path):
        parsed.append(path)
        return original(path)

    monkeypatch.setattr(analyzer, "parse_file", recording_parse)
    incremental, report = analyze_repository_incremental(repo, baseline)
    assert {path.relative_to(repo).as_posix() for path in parsed} == set(changed)
    assert report.parsed_files == 5
    assert report.reused_files == 2
    assert report.clustering == "affected communities"
    assert incremental.repo.commit.startswith(f"worktree:{baseline.repo.commit}:")

    monkeypatch.setattr(analyzer, "parse_file", original)
    clean_full = analyze_repository(repo)
    incremental_path = tmp_path / "incremental.json"
    full_path = tmp_path / "full.json"
    write_map(incremental, incremental_path)
    write_map(clean_full, full_path)
    assert incremental_path.read_bytes() == full_path.read_bytes()


def test_incremental_cli_requires_and_updates_existing_map(tmp_path: Path) -> None:
    repo = _git_fixture(tmp_path)
    map_path = repo / ".atlas" / "map.json"
    write_map(analyze_repository(repo), map_path)
    target = repo / "src" / "app.py"
    target.write_text(target.read_text() + "\n# changed\n")

    result = RUNNER.invoke(
        app,
        ["analyze", str(repo), "--incremental", "--output", str(map_path)],
    )

    assert result.exit_code == 0
    assert "parsed 1, reused 6; clustering=affected communities" in result.stdout
    assert json.loads(map_path.read_text())["repo"]["commit"].startswith("worktree:")


def test_dependency_change_reparses_one_file_and_reclusters_safely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _git_fixture(tmp_path)
    baseline = analyze_repository(repo)
    target = repo / "src" / "app.py"
    target.write_text("from auth.tokens import issue_token\n")
    parsed: list[Path] = []
    original = analyzer.parse_file

    def recording_parse(path: Path):
        parsed.append(path)
        return original(path)

    monkeypatch.setattr(analyzer, "parse_file", recording_parse)
    incremental, report = analyze_repository_incremental(repo, baseline)
    assert [path.relative_to(repo).as_posix() for path in parsed] == ["src/app.py"]
    assert report.clustering == "full (dependency weights changed)"

    monkeypatch.setattr(analyzer, "parse_file", original)
    assert incremental == analyze_repository(repo)


class ConfiguredProvider:
    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_model: type,
        **_: Any,
    ) -> ProviderResult:
        assert model == "recorded/from-config"
        assert os.environ["ATLAS_TEST_PROVIDER_KEY"] == "config-secret"
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        if response_model is ClusterEnrichment:
            records = payload["modules"]
            key = "modules"
        elif response_model is SystemEnrichment:
            records = payload["components"]
            key = "components"
        else:
            raise AssertionError(response_model)
        content = {
            key: [
                {
                    "id": record["id"],
                    "label": record["label"],
                    "summary": f"Configured summary for {record['id']}.",
                }
                for record in records
            ],
            "edge_labels": [
                {
                    "source": item["source"],
                    "target": item["target"],
                    "label": item.get("label") or "dependency",
                }
                for item in payload["context"]
                if item["kind"] == "edge"
            ],
        }
        return ProviderResult(json.dumps(content), model, 10, 5)


def test_config_uses_temporary_home_and_supplies_enrichment_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config_path = home / ".atlas" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "[enrichment]",
                'provider = "litellm"',
                'model = "recorded/from-config"',
                "budget = 0.25",
                "input_cost_per_million = 1.0",
                "output_cost_per_million = 2.0",
                "",
                "[provider_keys]",
                'ATLAS_TEST_PROVIDER_KEY = "config-secret"',
                "",
            ]
        )
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ATLAS_TEST_PROVIDER_KEY", raising=False)
    config = load_config()
    assert config.model == "recorded/from-config"
    with provider_environment(config.provider_keys):
        assert os.environ["ATLAS_TEST_PROVIDER_KEY"] == "config-secret"
    assert "ATLAS_TEST_PROVIDER_KEY" not in os.environ

    map_path = tmp_path / "map.json"
    output = tmp_path / "enriched.json"
    write_map(analyze_repository(FIXTURE), map_path)
    monkeypatch.setattr(
        "atlas_analyzer.cli.LiteLLMProvider", lambda: ConfiguredProvider()
    )
    result = RUNNER.invoke(
        app,
        [
            "enrich",
            str(map_path),
            "--no-cache",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0
    assert " / $0.250000" in result.stdout
    assert output.exists()
    assert "ATLAS_TEST_PROVIDER_KEY" not in os.environ
