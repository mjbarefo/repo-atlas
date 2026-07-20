# ATLAS

ATLAS is a local-first codebase map and agent activity visualizer. It
deterministically analyzes a repository into a layered architecture map,
replays Claude Code sessions on top of that map, and reviews change impact
against a Git base — all offline except one explicit, budget-capped LLM
enrichment command.

The repository contains a Python 3.12 analyzer CLI (`analyzer/`), a React
viewer (`viewer/`), and the JSON Schema contracts both sides are generated
from (`shared/schemas/`). See `AGENTS.md` for contributor conventions and
`plan.md` for the completed phase-gated build history.

## Workstation setup

ATLAS currently runs from a source checkout. Clone the private repository,
install the locked Python environment non-editably, and build the local viewer:

```bash
git clone git@github.com:mjbarefo/repo-atlas.git
cd repo-atlas

UV_CACHE_DIR=/tmp/atlas-uv-cache \
  uv sync --no-editable --reinstall-package atlas-analyzer
npm --prefix viewer ci
npm --prefix viewer run build
```

Use the checkout to analyze any local Git repository:

```bash
.venv/bin/atlas analyze /path/to/repository
.venv/bin/atlas serve /path/to/repository/.atlas/map.json \
  --repo-root /path/to/repository
```

Run `atlas serve` from the ATLAS checkout so it can find `viewer/dist`, or pass
`--viewer-dist /path/to/repo-atlas/viewer/dist` when launching elsewhere.
Python 3.12, `uv`, Node.js, and npm are required. Analysis and viewing remain
local; only the optional explicit enrichment command contacts a model provider.

## Updating an existing workstation

Bring a previously set-up checkout to the latest version:

```bash
git pull --ff-only origin main
make sync
npm --prefix viewer run build
```

`make sync` reinstalls the locked, non-editable Python environment and the
viewer's locked npm dependencies; the build step refreshes `viewer/dist` so
`atlas serve` serves the current viewer. Verify with `.venv/bin/atlas --help`
for a quick smoke test or `make check` for the full gate. In a Claude Code
session, `/update-workstation` runs this whole flow with preflight checks.

## Development

Run every check with one command:

```bash
make check
```

Or invoke the underlying steps directly:

```bash
uv sync --no-editable --reinstall-package atlas-analyzer
.venv/bin/python scripts/generate_models.py --check
.venv/bin/pytest
.venv/bin/black --check analyzer scripts
.venv/bin/ruff check analyzer scripts

npm --prefix viewer ci
npm --prefix viewer run check:generated
npm --prefix viewer run typecheck
npm --prefix viewer test
npm --prefix viewer run build
```

The JSON Schemas in `shared/schemas/` are the source of truth. Files under
`analyzer/src/atlas_analyzer/models/` and `viewer/src/generated/` are generated
and must not be edited manually.

## Static analysis

ATLAS deterministically analyzes Python, TypeScript, and JavaScript imports,
then groups files into heuristic module and component layers:

```bash
.venv/bin/atlas analyze /path/to/repository
```

The default artifact path is `<repository>/.atlas/map.json`; pass
`--output <path>` to override it. ATLAS respects the repository's `.gitignore`,
does not use the network during analysis, and emits evidence-backed import
edges with source file and line information.

After the initial map, update a Git worktree incrementally:

```bash
.venv/bin/atlas analyze /path/to/repository --incremental
```

The command diffs against the source commit recorded in `.atlas/map.json`,
parses modified files only, and reuses unaffected module/component communities.
Dependency-weight changes trigger a conservative full recluster while still
reparsing only the changed files. Added or deleted source files trigger a clean
full analysis because they can change how imports in otherwise unchanged files
resolve. Dirty-worktree artifacts receive a deterministic
`worktree:<base-commit>:<content-hash>` source version without changing schema
version 1.0.

The completed artifact is queryable without starting the viewer:

```bash
.venv/bin/atlas query deps file:src/auth/session.py
.venv/bin/atlas query rdeps file:src/auth/session.py
.venv/bin/atlas query cycles
.venv/bin/atlas query hotspots --repo /path/to/repository
```

Queries read `.atlas/map.json` by default; use `--map <path>` to select another
artifact. Hotspot scores are calculated from artifact fan-in and local Git
churn at query time, so repository history never changes the map artifact.

## Optional LLM enrichment

Enrichment is the only feature that needs a model provider, so its `litellm`
dependency is an optional extra that the default install skips entirely:

```bash
uv sync --no-editable --reinstall-package atlas-analyzer --extra enrichment
```

`atlas enrich` rewrites only module/component labels and summaries plus existing
edge labels. The command rejects structural changes before atomically replacing
an artifact. Pricing is explicit so the client can enforce the budget before
each provider call:

```bash
.venv/bin/atlas enrich .atlas/map.json \
  --provider litellm \
  --model openai/gpt-5-mini \
  --input-cost-per-million 0.25 \
  --output-cost-per-million 2.00 \
  --budget 0.50
```

Provider credentials come from the provider's standard environment variables.
Enrichment uses temperature zero, bounded prompts, structured responses, a
local cache, and one call per component plus one system-level call.

The same enrichment defaults can be stored in `~/.atlas/config.toml`; explicit
CLI flags take precedence. Provider keys are supplied only while `atlas enrich`
runs and never enter the offline analysis path:

```toml
[enrichment]
provider = "litellm"
model = "openai/gpt-5-mini"
budget = 0.50
input_cost_per_million = 0.25
output_cost_per_million = 2.00

[provider_keys]
OPENAI_API_KEY = "..."
```

Because this file can contain credentials, restrict it to your user account
(for example, `chmod 600 ~/.atlas/config.toml`). Use `--config <path>` to select
another file.

## Interactive viewer

Build the viewer, then serve a map on loopback:

```bash
npm --prefix viewer run build
.venv/bin/atlas serve /path/to/repository/.atlas/map.json
```

Open `http://127.0.0.1:4173`. The viewer uses layered ELK layouts at the
component, module, and file levels. Click a component and then a module to
reach its files; breadcrumbs return to either parent level. Selecting a node
shows its summary, metrics, descendant files, evidence-backed dependencies,
and `vscode://file/` source links. The current view can also be exported as
Mermaid.

When a map is outside its repository's `.atlas/` directory, pass
`--repo-root /path/to/repository` so editor links resolve to the source
checkout. Use the file picker when only local browser viewing is needed.

## Claude Code traces

Merge the hook definitions in `docs/claude-code-hooks.json` into a repository's
`.claude/settings.json`. The hook command resolves the script through
`${ATLAS_CHECKOUT:-$CLAUDE_PROJECT_DIR}`: inside the ATLAS checkout it works
as-is, and for any other repository set `ATLAS_CHECKOUT` to the ATLAS checkout
path (for example in the target repo's `.claude/settings.json` `env` block:
`"env": {"ATLAS_CHECKOUT": "/path/to/repo-atlas"}`).

The command hook records `Read`, `Edit`, `Write`,
`Grep`, and `Bash` calls under `.atlas/raw/<session>.jsonl`; recognized test
commands are classified as `Test`. Recorded Bash commands are scrubbed of
common credential shapes (authorization headers, `KEY=`/`TOKEN=` assignments,
password flags, and URL-embedded credentials) before they reach disk. Both hook phases are retained in the raw
log and paired by Claude's tool-use ID.

Resolve a completed raw session against its map:

```bash
.venv/bin/atlas ingest .atlas/raw/<session>.jsonl \
  --map .atlas/map.json \
  --repo .
```

The default output is `.atlas/traces/<session>.json`. Paths absent from the map
retain stable provisional `file:<normalized-path>` IDs.

Load a trace with the viewer:

```bash
.venv/bin/atlas serve .atlas/map.json \
  --trace .atlas/traces/<session>.json
```

The replay controls support timeline scrubbing and per-turn stepping. Reads
are cool, edits are warm, frequency controls intensity, and unread dependents
of edited nodes are outlined red.

For live mode, run the JSONL watcher in another terminal and select
**Connect live** in the viewer:

```bash
.venv/bin/atlas watch .atlas/raw/<session>.jsonl \
  --map .atlas/map.json \
  --repo .
```

Both HTTP and WebSocket services bind to loopback by default.

## Change impact review

After analyzing the current checkout, compare it with a local Git base:

```bash
.venv/bin/atlas impact /path/to/repository --base origin/main
```

The default `.atlas/impact.json` records every repository change, including
unsupported files and untracked worktree files. Untracked ATLAS runtime output
under `.atlas/` is excluded so the map, traces, and impact artifact do not
pollute their own review. Supported source files are joined to stable map node
IDs. Direct reverse dependencies come from the map's existing evidence-backed
edges, and the review order places changed dependencies before the changed
files that import them. The command is offline and resolves only local Git
refs.

Use `--head <ref>` for a committed base/head comparison. The selected map must
describe that head; ATLAS rejects mismatched artifacts instead of projecting
changes onto stale topology. Re-run `atlas analyze` (or the incremental form)
before comparing a changed worktree.

Load the result with the architecture map:

```bash
.venv/bin/atlas serve /path/to/repository/.atlas/map.json \
  --impact /path/to/repository/.atlas/impact.json
```

Changed files are projected through component, module, and file levels.
Evidence-backed direct dependents receive a separate risk outline, while the
review panel lists every changed path in dependency-first order with editor
links. If a compatible trace is also loaded, the panel marks mapped changed
files as edited, read, or unobserved by that session.

Phase 6 intentionally does not contact GitHub or mutate remote pull requests.
GitHub authentication, hosted review comments, LSP call graphs, multi-repo
stitching, and history animation remain separate post-MVP work.
