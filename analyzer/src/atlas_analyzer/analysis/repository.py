"""Repository walking and import resolution."""

from dataclasses import dataclass
import os
from pathlib import Path

import json5
from pathspec import GitIgnoreSpec

from .facts import ImportFact
from .languages import classify

IGNORED_DIRECTORIES = {
    ".atlas",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


def _spec_from_file(path: Path) -> GitIgnoreSpec | None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    return GitIgnoreSpec.from_lines(lines) if lines else None


IgnoreLayers = tuple[tuple[Path, GitIgnoreSpec], ...]


def _ignored(path: Path, layers: IgnoreLayers, *, is_dir: bool) -> bool:
    """Apply gitignore layers with git's precedence: the innermost matching
    pattern wins, so a nested .gitignore can re-include what a parent
    excluded (unless the parent directory itself was pruned)."""
    ignored = False
    for base, spec in layers:
        relative = path.relative_to(base).as_posix()
        if is_dir:
            relative += "/"
        include = spec.check_file(relative).include
        if include is not None:
            ignored = include
    return ignored


def source_files(root: Path) -> list[Path]:
    """Walk ROOT honoring nested .gitignore files and .git/info/exclude so
    discovery agrees with what git itself tracks; a file discovered here but
    invisible to git-based change detection would make incremental analysis
    silently stale."""
    root = root.resolve()
    result: list[Path] = []
    root_layers: list[tuple[Path, GitIgnoreSpec]] = []
    exclude_spec = _spec_from_file(root / ".git" / "info" / "exclude")
    if exclude_spec is not None:
        root_layers.append((root, exclude_spec))

    def walk(directory: Path, layers: IgnoreLayers) -> None:
        local = _spec_from_file(directory / ".gitignore")
        if local is not None:
            layers = (*layers, (directory, local))
        try:
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                if entry.name in IGNORED_DIRECTORIES:
                    continue
                if _ignored(entry, layers, is_dir=True):
                    continue
                walk(entry, layers)
            elif classify(entry) is not None and not _ignored(
                entry, layers, is_dir=False
            ):
                result.append(entry)

    walk(root, tuple(root_layers))
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


@dataclass(frozen=True)
class TsConfig:
    directory: Path
    base_url: Path
    paths: dict[str, tuple[str, ...]]


def _tsconfigs(root: Path) -> list[TsConfig]:
    # Prune ignored directories before descending: rglob would physically walk
    # all of node_modules/.git/dist on every analysis run just to discard the
    # results afterwards.
    found: list[Path] = []
    for current, directories, files in os.walk(root):
        directories[:] = sorted(
            name for name in directories if name not in IGNORED_DIRECTORIES
        )
        if "tsconfig.json" in files:
            found.append(Path(current) / "tsconfig.json")

    configs: list[TsConfig] = []
    for path in sorted(found):
        try:
            data = json5.loads(path.read_text())
        except (OSError, ValueError):
            continue
        compiler = data.get("compilerOptions", {})
        base_url = (path.parent / compiler.get("baseUrl", ".")).resolve()
        aliases = {
            key: tuple(value if isinstance(value, list) else [value])
            for key, value in compiler.get("paths", {}).items()
        }
        configs.append(TsConfig(path.parent.resolve(), base_url, aliases))
    return configs


def _existing_python_candidate(base: Path) -> Path | None:
    candidates = (
        [base]
        if base.suffix == ".py"
        else [base.with_suffix(".py"), base / "__init__.py"]
    )
    return next(
        (candidate.resolve() for candidate in candidates if candidate.is_file()), None
    )


def _existing_javascript_candidate(base: Path) -> Path | None:
    suffixes = (".ts", ".tsx", ".js", ".jsx")
    candidates: list[Path] = []
    if base.suffix in suffixes:
        candidates.append(base)
        candidates.extend(base.with_suffix(suffix) for suffix in suffixes)
    else:
        candidates.extend(base.with_suffix(suffix) for suffix in suffixes)
    candidates.extend(base / f"index{suffix}" for suffix in suffixes)
    return next(
        (candidate.resolve() for candidate in candidates if candidate.is_file()), None
    )


class ImportResolver:
    def __init__(self, root: Path, files: list[Path]) -> None:
        self.root = root.resolve()
        self.files = {path.resolve() for path in files}
        self.tsconfigs = _tsconfigs(self.root)

    def resolve(self, importer: Path, fact: ImportFact) -> Path | None:
        modules = (fact.module, *fact.fallbacks)
        for module in modules:
            if importer.suffix == ".py":
                resolved = self._resolve_python(importer.resolve(), module)
            else:
                resolved = self._resolve_javascript(importer.resolve(), module)
            if resolved in self.files:
                return resolved
        return None

    def _resolve_python(self, importer: Path, module: str) -> Path | None:
        level = len(module) - len(module.lstrip("."))
        parts = [part for part in module.lstrip(".").split(".") if part]
        if level:
            base = importer.parent
            for _ in range(level - 1):
                base = base.parent
            return _existing_python_candidate(base.joinpath(*parts))

        search_roots = [self.root]
        conventional_src = self.root / "src"
        if conventional_src.is_dir():
            search_roots.append(conventional_src)
        # Stay inside the repository: resolution is bounded by self.files
        # anyway, and probing ancestors above the root risks PermissionError
        # on restricted parent directories.
        search_roots.extend(
            parent for parent in importer.parents if parent.is_relative_to(self.root)
        )
        for search_root in dict.fromkeys(search_roots):
            candidate = _existing_python_candidate(search_root.joinpath(*parts))
            if candidate:
                return candidate
        return None

    def _config_for(self, importer: Path) -> TsConfig | None:
        containing = [
            config
            for config in self.tsconfigs
            if importer.is_relative_to(config.directory)
        ]
        return max(
            containing, key=lambda config: len(config.directory.parts), default=None
        )

    def _resolve_javascript(self, importer: Path, module: str) -> Path | None:
        if module.startswith("."):
            return _existing_javascript_candidate(importer.parent / module)

        config = self._config_for(importer)
        if config is None:
            return None
        for pattern, targets in sorted(config.paths.items()):
            if "*" in pattern:
                prefix, suffix = pattern.split("*", 1)
                if not module.startswith(prefix) or not module.endswith(suffix):
                    continue
                wildcard = module[
                    len(prefix) : len(module) - len(suffix) if suffix else None
                ]
            elif module == pattern:
                wildcard = ""
            else:
                continue
            for target in targets:
                candidate = _existing_javascript_candidate(
                    config.base_url / target.replace("*", wildcard)
                )
                if candidate:
                    return candidate
        return _existing_javascript_candidate(config.base_url / module)
