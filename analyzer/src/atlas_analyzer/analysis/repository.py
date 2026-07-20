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


def _ignore_spec(root: Path) -> GitIgnoreSpec:
    ignore_file = root / ".gitignore"
    lines = ignore_file.read_text().splitlines() if ignore_file.exists() else []
    return GitIgnoreSpec.from_lines(lines)


def source_files(root: Path) -> list[Path]:
    root = root.resolve()
    spec = _ignore_spec(root)
    result: list[Path] = []

    for current, directories, files in os.walk(root):
        current_path = Path(current)
        kept_directories: list[str] = []
        for directory in sorted(directories):
            candidate = current_path / directory
            relative = candidate.relative_to(root).as_posix() + "/"
            if directory not in IGNORED_DIRECTORIES and not spec.match_file(relative):
                kept_directories.append(directory)
        directories[:] = kept_directories

        for filename in sorted(files):
            path = current_path / filename
            relative = path.relative_to(root).as_posix()
            if classify(path) is not None and not spec.match_file(relative):
                result.append(path)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


@dataclass(frozen=True)
class TsConfig:
    directory: Path
    base_url: Path
    paths: dict[str, tuple[str, ...]]


def _tsconfigs(root: Path) -> list[TsConfig]:
    configs: list[TsConfig] = []
    for path in sorted(root.rglob("tsconfig.json")):
        if any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        data = json5.loads(path.read_text())
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
        search_roots.extend(importer.parents[:-1])
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
