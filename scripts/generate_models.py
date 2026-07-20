"""Generate committed Pydantic models from the canonical JSON Schemas."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "shared" / "schemas"
OUTPUT_DIR = ROOT / "analyzer" / "src" / "atlas_analyzer" / "models"


def generate(schema: Path, output: Path) -> None:
    subprocess.run(
        [
            str(Path(sys.executable).with_name("datamodel-codegen")),
            "--input",
            str(schema),
            "--input-file-type",
            "jsonschema",
            "--output",
            str(output),
            "--output-model-type",
            "pydantic_v2.BaseModel",
            "--target-python-version",
            "3.12",
            "--use-standard-collections",
            "--use-union-operator",
            "--disable-timestamp",
            "--formatters",
            "builtin",
        ],
        check=True,
        cwd=ROOT,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if committed generated models differ from the schemas.",
    )
    args = parser.parse_args()

    if args.check:
        with tempfile.TemporaryDirectory() as temporary:
            temp_dir = Path(temporary)
            for name in ("map", "trace", "impact"):
                generated = temp_dir / f"{name}.py"
                generate(SCHEMA_DIR / f"{name}.schema.json", generated)
                committed = OUTPUT_DIR / f"{name}.py"
                if (
                    not committed.exists()
                    or committed.read_bytes() != generated.read_bytes()
                ):
                    print(f"Generated model is stale: {committed.relative_to(ROOT)}")
                    return 1
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("map", "trace", "impact"):
        generate(SCHEMA_DIR / f"{name}.schema.json", OUTPUT_DIR / f"{name}.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
