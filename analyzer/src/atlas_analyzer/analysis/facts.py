"""Internal parser facts that are intentionally not part of map.json."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImportFact:
    module: str
    line: int
    fallbacks: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class SymbolTable:
    path: Path
    language: str
    definitions: tuple[str, ...]
    imports: tuple[ImportFact, ...]
    exports: tuple[str, ...]
    loc: int
