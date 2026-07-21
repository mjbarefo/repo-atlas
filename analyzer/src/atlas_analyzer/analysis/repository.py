"""Repository walking and import resolution."""

from dataclasses import dataclass
import os
from pathlib import Path
import sys

import json5
from pathspec import GitIgnoreSpec

from .facts import ImportFact, SymbolTable
from .languages import classify, parse_file, unparsable_table

# Top-level module names shipped with the standard library. A bare
# ``import logging`` means the stdlib module, not a same-named local file, so we
# decline to resolve it against the repository even when such a file exists.
_STDLIB_MODULES = frozenset(sys.stdlib_module_names)

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
    def __init__(
        self,
        root: Path,
        files: list[Path],
        tables: list[SymbolTable] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.files = {path.resolve() for path in files}
        self.tsconfigs = _tsconfigs(self.root)
        # Seed the symbol cache with already-parsed tables (full analysis) and
        # lazily parse everything else on demand (incremental analysis). Both
        # paths derive symbols from identical file content, so resolution — and
        # therefore the emitted edges — stay byte-identical between them.
        self._symbols: dict[Path, SymbolTable | None] = {
            table.path.resolve(): table for table in (tables or [])
        }
        self._base_roots = self._discover_search_roots()

    def _discover_search_roots(self) -> set[Path]:
        """Package roots to resolve absolute imports against. Instead of
        guessing ``root`` and ``root/src``, discover every ``src`` directory and
        every packaging-marker directory so a package under ``<sub>/src/pkg``
        (e.g. ``analyzer/src/atlas_analyzer``) is reachable from importers
        elsewhere in the repo."""
        roots = {self.root}
        directories: set[Path] = set()
        for path in self.files:
            for parent in path.parents:
                directories.add(parent)
                if parent == self.root or parent == parent.parent:
                    break
        for directory in directories:
            if directory.name == "src":
                roots.add(directory)
            if any(
                (directory / marker).is_file()
                for marker in ("pyproject.toml", "setup.py", "setup.cfg")
            ):
                roots.add(directory)
                if (directory / "src").is_dir():
                    roots.add(directory / "src")
        return roots

    def _table(self, path: Path) -> SymbolTable | None:
        # Degrade exactly as parse_source_file does (an unparsable file becomes
        # an import-free table, not None) so lazily parsed targets in the
        # incremental path resolve identically to the pre-seeded full path.
        resolved = path.resolve()
        if resolved not in self._symbols:
            try:
                self._symbols[resolved] = parse_file(resolved)
            except (SyntaxError, UnicodeDecodeError, ValueError):
                self._symbols[resolved] = unparsable_table(
                    resolved, classify(resolved) or "unknown"
                )
            except OSError:
                self._symbols[resolved] = None
        return self._symbols[resolved]

    def _provides(self, path: Path, symbols: tuple[str, ...]) -> bool:
        """Whether PATH plausibly defines or re-exports any of SYMBOLS. An
        unreadable file is treated as providing them so an I/O gap never drops a
        real edge."""
        table = self._table(path)
        if table is None:
            return True
        provided = set(table.definitions) | set(table.exports)
        provided.update(symbol for fact in table.imports for symbol in fact.symbols)
        return any(symbol in provided for symbol in symbols)

    def resolve(self, importer: Path, fact: ImportFact) -> Path | None:
        importer = importer.resolve()
        if importer.suffix == ".py":
            return self._resolve_python(importer, fact)
        for module in (fact.module, *fact.fallbacks):
            resolved = self._resolve_javascript(importer, module)
            if resolved in self.files:
                return resolved
        return None

    def _resolve_python(self, importer: Path, fact: ImportFact) -> Path | None:
        symbols = tuple(symbol for symbol in fact.symbols if symbol and symbol != "*")

        # A bare ``import X`` (no ``from`` clause) for a stdlib top-level name is
        # the standard library, never a same-named local file that happens to
        # share the name. from-imports are left to symbol verification below.
        if (
            not fact.fallbacks
            and "." not in fact.module
            and fact.module in _STDLIB_MODULES
        ):
            return None

        # 1. The primary module resolves directly when it names a real module or
        #    submodule file (``import a.b`` / ``from a import b`` where b is a
        #    submodule). That target is unambiguous, so accept the closest match.
        primary = self._closest_candidate(importer, fact.module)
        if primary is not None:
            return primary

        # 2. ``from <base> import <symbol>``: resolve the base package/module.
        #    A relative import names exactly one target, so trust it. An absolute
        #    import can misresolve across search roots, so require the resolved
        #    file to actually provide the symbol and otherwise drop the edge
        #    rather than point it at the wrong same-named file.
        if fact.fallbacks:
            for base in fact.fallbacks:
                candidates = self._python_candidates(importer, base)
                if not candidates:
                    continue
                if base.startswith(".") or not symbols:
                    chosen = candidates[0]
                else:
                    chosen = next(
                        (
                            candidate
                            for candidate in candidates
                            if self._provides(candidate, symbols)
                        ),
                        None,
                    )
                    if chosen is None:
                        return None
                return self._follow_barrel(chosen, symbols, {importer, chosen})
        return None

    def _search_roots(self, importer: Path) -> set[Path]:
        roots = set(self._base_roots)
        roots.update(
            parent for parent in importer.parents if parent.is_relative_to(self.root)
        )
        return roots

    def _python_candidates(self, importer: Path, module: str) -> list[Path]:
        level = len(module) - len(module.lstrip("."))
        parts = [part for part in module.lstrip(".").split(".") if part]
        if level:
            base = importer.parent
            for _ in range(level - 1):
                base = base.parent
            candidate = _existing_python_candidate(base.joinpath(*parts))
            return [candidate] if candidate in self.files else []
        if not parts:
            return []

        found = {
            candidate
            for search_root in self._search_roots(importer)
            if (candidate := _existing_python_candidate(search_root.joinpath(*parts)))
            in self.files
        }
        found.discard(importer)

        def closeness(path: Path) -> tuple[int, str]:
            shared = 0
            for left, right in zip(path.parts, importer.parts):
                if left != right:
                    break
                shared += 1
            return (-shared, path.as_posix())

        return sorted(found, key=closeness)

    def _closest_candidate(self, importer: Path, module: str) -> Path | None:
        candidates = self._python_candidates(importer, module)
        return candidates[0] if candidates else None

    def _follow_barrel(
        self,
        path: Path,
        symbols: tuple[str, ...],
        visited: set[Path],
        depth: int = 0,
    ) -> Path:
        """Re-point an edge that landed on a re-exporting ``__init__.py`` barrel
        to the module that actually defines the symbol, so consumers point at
        the source of a dependency rather than the package facade."""
        if not symbols or path.name != "__init__.py" or depth >= 5:
            return path
        table = self._table(path)
        if table is None:
            return path
        defined = set(table.definitions) | set(table.exports)
        if any(symbol in defined for symbol in symbols):
            return path
        for fact in sorted(table.imports, key=lambda item: (item.line, item.module)):
            if set(fact.symbols) & set(symbols):
                target = self._resolve_python(path, fact)
                if target is not None and target not in visited:
                    return self._follow_barrel(
                        target, symbols, visited | {target}, depth + 1
                    )
        return path

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
