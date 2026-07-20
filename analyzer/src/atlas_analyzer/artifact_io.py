"""Atomic artifact writes shared by analysis, impact, ingestion, and enrichment."""

from __future__ import annotations

from pathlib import Path
import tempfile


def atomic_write_text(destination: Path, payload: str) -> None:
    """Write PAYLOAD to DESTINATION atomically: a crash or full disk mid-write
    must never leave a truncated artifact that every later command fails to
    parse."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        mode="w",
        delete=False,
    ) as temporary_file:
        temporary_file.write(payload)
        temporary = Path(temporary_file.name)
    try:
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
