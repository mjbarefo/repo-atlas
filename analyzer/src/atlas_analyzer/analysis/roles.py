"""Deterministic provenance classification for repository files.

A file node's ``role`` records whether it is production source or one of
several non-source classes (test, fixture, generated, vendored). Only role
``"source"`` files form and name modules/components; everything else stays in
the map but is excluded from architecture layering. Classification is a pure
function of the repo-relative POSIX path plus offline, user-supplied overrides,
so full and incremental analyses assign identical roles and stay byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass

from pathspec import GitIgnoreSpec

from atlas_analyzer.config import AnalysisConfig

SOURCE = "source"
FIXTURE_DIRECTORIES = frozenset({"fixtures", "testdata"})
GENERATED_DIRECTORIES = frozenset({"generated", "__generated__"})
TEST_DIRECTORIES = frozenset({"tests"})
TEST_FILE_SUFFIXES = (".test.ts", ".test.tsx", ".test.js", ".test.jsx")


def _spec(patterns: tuple[str, ...]) -> GitIgnoreSpec | None:
    if not patterns:
        return None
    try:
        return GitIgnoreSpec.from_lines(patterns)
    except ValueError as error:
        # pathspec raises a ValueError subclass on a malformed gitignore pattern;
        # surface it as a plain ValueError so callers (the CLI) can turn it into
        # a clean usage error instead of an unhandled traceback.
        raise ValueError(f"invalid analysis path pattern: {error}") from error


@dataclass(frozen=True)
class RoleClassifier:
    """Assign a ``role`` to a repo-relative path.

    Config-driven overrides (gitignore-style patterns) take precedence over the
    built-in path conventions; both are deterministic and offline.
    """

    generated: GitIgnoreSpec | None = None
    vendored: GitIgnoreSpec | None = None

    def role_for(self, relative_path: str) -> str:
        if self.generated is not None and self.generated.match_file(relative_path):
            return "generated"
        if self.vendored is not None and self.vendored.match_file(relative_path):
            return "vendored"
        parts = relative_path.split("/")
        directories = frozenset(parts[:-1])
        name = parts[-1]
        if directories & FIXTURE_DIRECTORIES:
            return "fixture"
        if directories & GENERATED_DIRECTORIES:
            return "generated"
        if directories & TEST_DIRECTORIES:
            return "test"
        if name.startswith("test_") and name.endswith(".py"):
            return "test"
        if name.endswith(TEST_FILE_SUFFIXES):
            return "test"
        return SOURCE


def build_role_classifier(config: AnalysisConfig) -> RoleClassifier:
    """Compile a classifier from the offline ``[analysis]`` configuration."""
    return RoleClassifier(_spec(config.generated), _spec(config.vendored))
