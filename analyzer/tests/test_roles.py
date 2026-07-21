import pytest

from atlas_analyzer.analysis.roles import RoleClassifier, build_role_classifier
from atlas_analyzer.config import AnalysisConfig, load_analysis_config


def test_path_conventions_classify_non_source() -> None:
    classifier = RoleClassifier()

    assert classifier.role_for("analyzer/src/atlas_analyzer/analysis/analyzer.py") == (
        "source"
    )
    assert classifier.role_for("web/main.ts") == "source"
    assert classifier.role_for("analyzer/tests/test_static_analysis.py") == "test"
    assert classifier.role_for("viewer/src/graph.test.ts") == "test"
    assert classifier.role_for("viewer/src/graph.test.tsx") == "test"
    assert classifier.role_for("viewer/src/generated/map.ts") == "generated"
    assert classifier.role_for("pkg/__generated__/models.py") == "generated"
    assert classifier.role_for("analyzer/tests/fixtures/golden_repo/src/app.py") == (
        "fixture"
    )
    assert classifier.role_for("pkg/testdata/rows.py") == "fixture"


def test_fixtures_under_tests_are_fixtures_not_tests() -> None:
    # A file under tests/fixtures/ is a fixture: the more specific directory
    # wins so golden repositories never pollute the "test" bucket.
    classifier = RoleClassifier()
    assert classifier.role_for("analyzer/tests/fixtures/repo/main.py") == "fixture"


def test_config_overrides_take_precedence_over_path_conventions() -> None:
    config = AnalysisConfig(
        generated=("analyzer/src/atlas_analyzer/models/",),
        vendored=("third_party/",),
    )
    classifier = build_role_classifier(config)

    assert classifier.role_for("analyzer/src/atlas_analyzer/models/map.py") == (
        "generated"
    )
    assert classifier.role_for("third_party/dep/client.py") == "vendored"
    assert classifier.role_for("analyzer/src/atlas_analyzer/analysis/analyzer.py") == (
        "source"
    )


def test_load_analysis_config_reads_analysis_table(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[analysis]",
                'generated = ["analyzer/src/atlas_analyzer/models/"]',
                'vendored = ["third_party/"]',
            ]
        )
    )

    config = load_analysis_config(config_path)

    assert config.generated == ("analyzer/src/atlas_analyzer/models/",)
    assert config.vendored == ("third_party/",)


def test_load_analysis_config_missing_explicit_path_errors(tmp_path) -> None:
    with pytest.raises(ValueError):
        load_analysis_config(tmp_path / "missing.toml")


def test_load_analysis_config_without_analysis_table_is_empty(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[enrichment]\nmodel = "recorded/x"\n')

    assert load_analysis_config(config_path) == AnalysisConfig()
