# Repository Guidelines

## Project Structure & Module Organization

ATLAS is a local-first monorepo with two applications and shared artifact contracts:

- `analyzer/src/atlas_analyzer/`: Python 3.12 CLI, static analysis, enrichment, trace ingestion, serving, and impact analysis.
- `analyzer/tests/`: pytest suites; reusable repository inputs live in `analyzer/tests/fixtures/`.
- `viewer/src/`: React and TypeScript viewer, with Vitest tests beside the modules they cover.
- `shared/schemas/`: authoritative JSON Schemas; `shared/fixtures/` contains cross-language examples.
- `scripts/`: model generation and end-to-end validation utilities.
- `plan.md`: the completed phase-gated build history (Phases 0–6) and the list of deferred post-MVP scope; background reading, not live instructions.

Do not manually edit `analyzer/src/atlas_analyzer/models/{map,trace,impact}.py` or `viewer/src/generated/`; regenerate them from the schemas.

## Build, Test, and Development Commands

`make sync` installs both locked environments; `make check` runs every quality
gate (also enforced by `.github/workflows/ci.yml`). Invoke the installed CLI
and tools directly as `.venv/bin/atlas`, `.venv/bin/pytest`, etc. — do not use
`uv run`; editable installs are unreliable on this project's runtime because
hidden `.pth` files are skipped, which is why the install is non-editable. The
underlying commands:

```bash
uv sync --no-editable --reinstall-package atlas-analyzer
.venv/bin/pytest
.venv/bin/python scripts/generate_models.py --check
.venv/bin/black --check analyzer scripts
.venv/bin/ruff check analyzer scripts
npm --prefix viewer ci
npm --prefix viewer test
npm --prefix viewer run check:generated
npm --prefix viewer run typecheck
npm --prefix viewer run build
npm --prefix viewer run dev
```

The checks validate Python tests, formatting/lint, generated bindings, viewer
tests, and the production TypeScript/Vite build. Run
`.venv/bin/atlas analyze /path/to/repo` for a local CLI smoke test. The
non-editable install leaves a `build/` directory at the repo root as a
setuptools side effect; it is gitignored — never edit or grep it as if it
were source.

## Validation Scripts

End-to-end gates beyond the unit suites, all run with `.venv/bin/python`
(they import the installed `atlas_analyzer`):

- `scripts/validate_incremental.py <git-repo>` — copies the repo, edits five
  files, and asserts incremental analysis is <10% of full runtime with a
  byte-identical artifact.
- `scripts/validate_impact.py <git-repo>` — builds a disposable 12-change
  comparison and asserts impact output matches `git diff --name-status`.
- `scripts/validate_recorded_enrichment.py <map> <output>` — offline
  enrichment run against a recorded provider; proves budget and structural
  invariants without network access.
- `scripts/generate_viewer_perf_fixture.py <output>` — deterministic 500-file
  map for browser rendering checks.

## Coding Style & Naming Conventions

Python uses four-space indentation, type annotations, `snake_case` functions/modules, and `PascalCase` classes. Format Python with Black; its configuration excludes generated models. Ruff targets Python 3.12 and excludes generated code and test fixtures. TypeScript uses two spaces, semicolons, `camelCase` functions, and `PascalCase` React components. Keep analysis deterministic: sort filesystem- or graph-derived output and avoid network access in the core analyzer.

## Testing Guidelines

Name Python tests `test_*.py` and TypeScript tests `*.test.ts` or `*.test.tsx`. Add focused regression coverage near the affected subsystem. Schema changes require updated schemas, generated Python and TypeScript bindings, fixtures, and contract tests. Run both language test suites before opening a pull request.

## Commit & Pull Request Guidelines

Use a concise imperative subject and keep each commit focused on one theme (see the post-MVP history for examples: analyzer correctness, server hardening, viewer fixes, and tooling landed as separate commits). Pull requests should explain intent, list verification commands, link relevant issues, and call out schema or generated-file changes. Include screenshots for viewer changes and document any deferred validation.

## Dependency Pinning

Python dependencies use minimum-only pins (`>=x`, no upper bounds) — minimums
sit at security-patched floors and `uv.lock` governs the installed versions.
Do not add upper bounds; upgrades happen through a deliberate `uv lock
--upgrade` vetted by the full check suite.

## Security & Configuration

Never commit provider credentials, `.atlas/` runtime artifacts, `.venv/`, or `viewer/node_modules/`. Keep enrichment credentials in a permission-restricted `~/.atlas/config.toml`; offline analysis must remain network-free.
