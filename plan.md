# ATLAS — Codebase Map + Agent Activity Visualizer
## End-to-End Build Plan (for delegation to Opus / GPT 5.6)

> **Status (2026-07-20): historical record.** Phases 0–6 are complete with
> verified exit evidence recorded inline below. This document is no longer
> live instructions for an agent session; treat it as the build history and
> the authoritative list of deferred post-MVP scope (section 5). Current
> contributor conventions live in `AGENTS.md`; current usage lives in
> `README.md`.

This document is written to be pasted directly into an agentic coding session as the master spec. It follows a schema-first, phase-gated structure: every phase has explicit exit criteria the agent must demonstrate before proceeding. Do not let the agent skip gates.

---

## 0. Problem Statement & Rationale

**What we're building:** A local-first tool that (a) statically analyzes a codebase into a layered, interactive architecture map, and (b) overlays live/replayed agentic coding activity (Claude Code sessions) onto that map — showing which components an agent read, edited, tested, and in what order.

**Why not just use CodeBoarding:** Its open-source CLI covers (a). It does not cover (b), and (b) is the high-value part: reviewing agent work spatially ("it edited the auth module but never read the session store it depends on") instead of as a linear transcript.

**Design rationale (state this to the agent):**
1. **Artifact-first architecture.** All analysis produces a versioned JSON artifact; the viewer only ever reads artifacts. This decouples analysis cost from viewing, enables replay/determinism, and mirrors proven patterns (analysis.json in CodeBoarding; same principle as LANTERN's artifact schema).
2. **Deterministic core, optional LLM enrichment.** The entire pipeline — parse, graph, cluster, name, render — runs with zero AI calls (`atlas analyze` default). Structure comes from parsers and graph algorithms; the LLM is a strictly optional *enrichment pass* (`--enrich=llm`) that rewrites labels/summaries on an already-complete artifact. LLMs hallucinate edges; parsers don't — so the LLM never touches graph topology, only prose. This means the tool works offline, in CI, on private code, and costs $0 by default.
3. **Agent activity is an event stream joined to the graph by file path.** Claude Code hooks emit tool-use events (Read/Edit/Write/Bash) as JSONL; each event resolves to a graph node. No new analysis needed — it's a projection.

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  CLI (Python, Typer)                                     │
│  atlas analyze <repo> · query · serve · watch · enrich   │
└──────┬──────────────────────────────┬───────────────────┘
       │                              │
┌──────▼──────────────┐    ┌──────────▼──────────────────┐
│ ANALYZER (Python)   │    │ TRACE INGESTOR (Python)      │
│ 1. tree-sitter parse│    │ - Claude Code hooks → JSONL  │
│ 2. import/call graph│    │ - resolve file → node_id     │
│ 3. algo clustering  │    │ - session timeline events    │
│ 4. heuristic naming │    │ - emit trace artifact        │
│ 5. emit artifact    │    └──────────┬──────────────────┘
│ [opt] --enrich=llm  │               │
│   rewrites prose    │               │
       │                              │
       ▼                              ▼
  .atlas/map.json              .atlas/traces/<session>.json
       │                              │
       └──────────┬───────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────┐
│ VIEWER (React + Vite, single static bundle)              │
│ - SVG graph render (dagre/elk layout), Canvas for        │
│   heatmap/animation layers                               │
│ - drill-down: system → component → module → file         │
│ - trace replay: scrubber, heatmap, read/write coloring   │
│ - served by `atlas serve` (FastAPI static + WebSocket    │
│   for live mode)                                         │
└─────────────────────────────────────────────────────────┘
```

**Stack choices and why:**
- **tree-sitter** over LSP servers for v1: zero external binaries, one API across Python/TS/JS, fast. LSP-grade call resolution is a v2 upgrade path. (Trade-off: tree-sitter gives syntactic imports/defs, not fully resolved call graphs — acceptable; import + same-file call edges cover ~90% of architectural signal.)
- **React + SVG for graph, Canvas for overlays** — same hybrid you're using in LANTERN; skills and components transfer directly. SVG nodes stay clickable/accessible; Canvas handles the high-frequency heatmap/replay layer without React re-render cost.
- **FastAPI** only as a thin static-file + WebSocket server. All heavy work happens in the CLI ahead of time (artifact-first), so no worker-queue problem here — the one lesson from LANTERN's event-loop issue applied preemptively.
- **Mermaid export** as a secondary output (for embedding in docs/PRs), never the primary render — Mermaid can't do the interactive overlay.

---

## 2. The Artifact Schemas (define FIRST — Phase 0)

### 2.1 `map.json` — the codebase map

```jsonc
{
  "schema_version": "1.0",
  "repo": { "root": ".", "commit": "<git sha>", "generated_at": "ISO8601" },
  "nodes": [
    {
      "id": "comp:auth",              // stable, content-addressed where possible
      "kind": "component | module | file",
      "label": "Authentication",
      "summary": "1-2 sentences",
      "prose_source": "heuristic | llm",   // provenance per node — enrichment overwrites prose, never structure
      "children": ["mod:auth.session", "mod:auth.tokens"],
      "files": ["src/auth/session.py"], // leaf nodes only
      "metrics": { "loc": 1240, "fan_in": 3, "fan_out": 5 }
    }
  ],
  "edges": [
    {
      "source": "comp:auth", "target": "comp:db",
      "kind": "imports | calls | inherits",
      "evidence": [ { "file": "src/auth/session.py", "line": 12 } ],  // REQUIRED — no evidence, no edge
      "label": "SessionStore, verify_token"  // heuristic: crossed symbols; LLM enrichment may rewrite to prose
    }
  ],
  "levels": { "system": ["comp:*"], "component": {...} }
}
```

**Rationale:** the `evidence` field is the anti-hallucination contract — the LLM may relabel or cluster, but cannot invent edges. Stable node IDs enable trace joins and incremental re-analysis.

### 2.2 `trace.json` — an agent session

```jsonc
{
  "schema_version": "1.0",
  "session_id": "cc-2026-07-12-a",
  "agent": "claude-code",
  "map_ref": { "commit": "<sha map was built at>" },
  "events": [
    {
      "t": 0.0,                      // seconds from session start
      "tool": "Read | Edit | Write | Bash | Grep | Test",
      "path": "src/auth/session.py",
      "node_id": "file:src/auth/session.py",  // resolved at ingest
      "detail": { "lines_changed": 14 },       // tool-specific
      "turn": 3                       // agent turn number
    }
  ],
  "summary": { "files_read": 12, "files_edited": 3, "tests_run": 2 }
}
```

---

## 3. Phase Plan with Exit Criteria

Direct the agent to work strictly in this order. Each phase ends with a demo the agent runs and shows output from.

### Phase 0 — Scaffold & Contracts (half day)
- Monorepo: `analyzer/` (Python 3.12, uv), `viewer/` (Vite + React + TS), `shared/schemas/` (JSON Schema files for both artifacts).
- Pydantic models generated/validated against the JSON Schemas; TS types generated via `json-schema-to-typescript`. One source of truth.
- **Exit criteria:** `pytest` proves a hand-written sample `map.json` round-trips (parse → model → serialize → byte-identical modulo key order) in BOTH Python and TS. This is the same round-trip gate you defined for LANTERN Phase 1 — it works, reuse it.

### Phase 1 — Static Analysis Core (1–2 days)
Programmatic steps:
1. Walk repo (respect `.gitignore`), classify files by language (py/ts/js first).
2. tree-sitter parse → per-file symbol table (defs, imports, exports).
3. Resolve imports to files (handle relative imports, `__init__.py`, TS path aliases from tsconfig).
4. Emit file-level graph: nodes = files, edges = imports with `evidence` (file+line).
5. Compute metrics: LOC, fan-in/out.
- **No LLM in this phase.** Deterministic output: same repo + commit → identical artifact.
- **Exit criteria:** run on a known mid-size OSS repo (e.g., `fastapi` itself, or LANTERN); assert node count matches file count, spot-check 10 edges against source by hand, and two consecutive runs produce identical artifacts (determinism gate).

### Phase 2a — Deterministic Abstraction Layer (1–2 days) ← default path, zero AI
Programmatic steps:
1. **Cluster:** community detection on the file graph (greedy modularity or Leiden with fixed seed), *constrained by directory structure* — a cluster may only merge files sharing a common ancestor directory unless edge density between them exceeds a threshold. Rationale: pure graph clustering produces mathematically valid but humanly weird groupings; directory constraints anchor clusters to how the author already organized the code.
2. **Name:** heuristic pipeline, in priority order: (a) longest common path prefix (`src/auth/**` → "auth"); (b) package name from `__init__.py` / `package.json` / `index.ts`; (c) highest-fan-in module's name in the cluster. Title-case and de-duplicate.
3. **Summarize:** extract, don't generate — first line of package/module docstring, else README heading in that directory, else a generated stat line ("14 files, exports SessionStore, TokenValidator; imported by api, cli"). Tag `prose_source: heuristic`.
4. **Component edges:** roll up file edges between clusters; edge label = the top symbol names actually imported across the boundary (e.g., "SessionStore, verify_token") — real identifiers, not prose.
5. **Graph queries (CLI):** `atlas query deps <node>`, `rdeps <node>`, `cycles`, `hotspots` (fan-in × churn) — the graph is useful from the terminal before the viewer exists, and gives agents a queryable codebase index.
- **Exit criteria:** full `map.json` from the test repo with NO network access (assert via blocked-socket test); two runs byte-identical; you personally judge cluster names sensible for ≥80% of components on the golden repo.

### Phase 2b — Optional LLM Enrichment (1 day, can be deferred indefinitely)
`atlas enrich --provider=...` takes a finished `map.json` and rewrites only `label`, `summary`, and edge `label` fields (provider-agnostic via litellm; one call per cluster with capped context, one system-level pass). Sets `prose_source: llm` per touched node.
- **Hard validator:** enrichment output is diffed against input — any change outside prose fields is rejected. Topology is immutable to the LLM by construction.
- **Rationale:** enrichment as a separate command on a complete artifact (rather than a pipeline stage) means the deterministic path is never entangled with provider code, and you can enrich once, cheaply, after the map stabilizes.
- **Exit criteria:** enrich the golden repo map; validator proves zero structural diffs; cost printed and under budget (e.g., <$0.50 per 50k LOC); `atlas analyze` still passes the no-network test.

### Phase 3 — Viewer (2–3 days)
1. Load artifact (file picker + `atlas serve` endpoint).
2. Layout: elkjs (layered) at each drill level; SVG nodes/edges; click component → expand children (breadcrumb up).
3. Node detail panel: summary, files, metrics, evidence-backed edge list with jump-to-source links (`vscode://file/...`).
4. Mermaid export button per view.
- **Exit criteria:** open test-repo map, drill system → component → file in <3 clicks, 60fps pan/zoom on a 500-node level (Canvas fallback for edges if SVG chokes).
- **Verified 2026-07-20:** Browser QA at 1280×720 (2× DPR) rendered all 13 FastAPI system components with 0 node overlaps and 0 clipped nodes. System → JS → JS reached the 3-file level in exactly 2 clicks; module/component/system breadcrumbs, node keyboard activation, detail summaries/files/LOC/fan-in/fan-out/evidence, `vscode://file` links, pan, wheel zoom, zoom buttons, fit reset, JSON picker, and Mermaid `.mmd` content all passed. Desktop visual inspection and both viewer consoles were clean.
- **500-node evidence:** Performance Gate → 500 File Level reached 500 files in exactly 2 clicks with 1 Canvas edge layer and 0 SVG edge polylines. A 6,001.5 ms `requestAnimationFrame` recording during continuous browser-driven pan/zoom captured 357 frames: 59.49 average FPS, 18.10 ms p95 frame time, and 3 dropped frames (60 Hz missed-frame calculation) at 1280×720. Interaction remained visually smooth; after pan/zoom, `file_000.py` remained legible and clickable and opened its detail/evidence panel.
- **Command gate:** `.venv/bin/pytest` (30 passed), `npm --prefix viewer test` (12 passed), Black check (28 files unchanged), generated Python models check, generated viewer types check, and production viewer build all passed.

### Phase 4 — Agent Trace Overlay (2 days) ← the differentiator
1. **Capture:** Claude Code hooks (`PreToolUse`/`PostToolUse` in `.claude/settings.json`) append JSONL events (tool, path, timestamp, turn) to `.atlas/raw/<session>.jsonl`. Ship the hook script + settings snippet.
2. **Ingest:** `atlas ingest` resolves paths → node IDs against current map, emits `trace.json`. Unresolvable paths (new files agent created) get provisional nodes, visually distinct.
3. **Render:** heatmap layer on Canvas (read = cool, edit = warm, intensity = frequency); timeline scrubber replaying the session over the map; per-turn stepping.
4. **Live mode:** `atlas watch` tails the JSONL and pushes over WebSocket — watch the agent move through your architecture in real time.
- **Exit criteria:** record a real Claude Code session on the test repo, replay it, and answer visually: "which components did it modify without reading their dependents?" (render dependents-of-edited-nodes that have zero Read events in red).
- **Verified 2026-07-20:** Browser QA replayed trace `d8415cf9-1fd3-4c68-85da-0d5d5b4134dc` with 9 events/9 turns. Before reading labels, the two warm edited components and five red-outlined unread dependents were visually identifiable; inspection confirmed edits in Exceptions and Models, with unread dependents Docs Src, Param Functions, Responses, Staticfiles, and Tests 2. Reads rendered cool blue, edits warm orange, timeline scrub from 0.0 to 69.6 seconds revealed activity progressively, turn stepping reached Turn 0 → Turn 1 → Turn 0, and edit heat projected through Exceptions → Fastapi 2 → `applications.py`.
- **Live/provisional evidence:** `Connect live` reached the connected state and loaded the same resolved 9-event snapshot from `ws://127.0.0.1:8765`. The separate fixture rendered 10 events and kept `fastapi/generated_probe.py` outside mapped nodes in a purple 1 px dashed Provisional treatment. Both browser consoles were clean.
- **Command gate:** `.venv/bin/pytest` (34 passed), `npm --prefix viewer test` (15 passed), Black check (33 files unchanged), generated Python models check, generated viewer types check, and production viewer build all passed.

### Phase 5 — Incremental & Polish (1–2 days)
- `atlas analyze --incremental`: diff changed files since `map.json`'s commit, re-parse only those, re-cluster only affected communities, bump artifact version.
- Config file (`~/.atlas/config.toml`): provider keys, model, budget cap.
- **Exit criteria:** incremental run on a 5-file change completes in <10% of full-run time with an identical-except-affected artifact.
- **Verified 2026-07-20:** `scripts/validate_incremental.py` copied the FastAPI golden checkout into a disposable worktree, changed exactly five source files, and produced a byte-identical incremental/clean-full artifact (`sha256 0c7578f1a6a737e146ddef7075de258f7c9ef42747559a70fe9f623dfb98e15b`). The incremental path parsed 5 files, reused 1,126, refreshed only affected stable-weight communities, and completed in 0.659806 seconds versus 10.072203 seconds for clean full analysis (6.5508%). The artifact source version advanced deterministically to `worktree:<base-commit>:<content-hash>` while schema version 1.0 remained unchanged.
- **Config evidence:** tests redirected `HOME` to a temporary directory, loaded provider/model/pricing/budget/key defaults from `.atlas/config.toml`, proved explicit enrichment isolation and temporary credential cleanup, and did not read or mutate the real user configuration. Dependency-topology regression coverage proves only the edited file is reparsed while clustering safely falls back to a deterministic rebuild.
- **Command gate:** `.venv/bin/pytest` (38 passed), `npm --prefix viewer test` (15 passed), Black check (36 files unchanged), both generated-binding checks, CLI help for the new options, and the production viewer build all passed. Vite emitted only its existing non-blocking large-chunk warning.

### Phase 6 — Change Impact Review (2–3 days)
1. **Compare:** `atlas impact <repo> --base <ref>` compares a local Git base with either the current worktree or an explicit `--head <ref>`. It records every added, modified, deleted, renamed, copied, or type-changed repository path and includes untracked worktree files without requiring GitHub credentials or network access. Untracked ATLAS runtime output under `.atlas/` is excluded from its own report.
2. **Artifact:** emit a separate, deterministic `.atlas/impact.json` v1.0 joined to the existing map by `map_ref.commit`. Keep `map.json` schema 1.0 frozen. The impact artifact carries exact file status, stable node IDs where the current map contains the path, direct reverse-dependency pairs, a dependency-first review order, and summary counts.
3. **Render:** load the impact artifact alongside the map, project changed files and direct dependents through component/module/file levels, distinguish change kinds visually, and expose a review panel with ordered source links.
4. **Trace join:** when a compatible trace is loaded, summarize which changed mapped files the agent read or edited and which changed files remain unobserved. Trace capture remains optional and does not alter the impact artifact.
5. **Scope fence:** local Git refs/worktrees only. Do not add GitHub authentication/API access, hosted PR comments, LSP call resolution, multi-repo stitching, or commit-history animation in this phase.
- **Exit criteria:** on a real 10–30-file comparison, impact status matches `git diff --name-status` plus eligible untracked files exactly (excluding `.atlas/` runtime output); two runs emit byte-identical artifacts; every mapped changed file appears in the deterministic review order; direct dependents are evidence-backed by existing map edges; the viewer reaches every changed file in at most three interactions and visibly answers which architectural regions changed, which direct dependents are at risk, and which changed files the optional agent trace never observed.
- **Verified backend 2026-07-20:** `scripts/validate_impact.py` copied the FastAPI golden checkout into a disposable repository, changed exactly 12 paths (9 modified, 1 renamed, 1 deleted, 1 added), and matched independent `git diff --name-status --find-renames` plus eligible untracked discovery exactly. The artifact mapped 11 current files, retained the deleted path as unmapped, emitted all 12 paths once in dependency-first review order, found 716 evidence-backed change/dependent pairs across 541 distinct direct-dependent file nodes, and was byte-identical across two builds. The source checkout remained unchanged.
- **Automated viewer evidence:** impact projection tests prove change status and direct-dependent risk roll up through system and file levels; trace-join tests distinguish edited/read/unobserved changed files; contract tests round-trip `impact.json` v1.0 in Python and TypeScript. The production viewer loads a same-commit impact through `/api/impact`, rejects server-side map mismatches, renders distinct change/risk treatments, and exposes direct editor links for every review-order entry.
- **Command gate:** non-editable `uv` reinstall passed; `.venv/bin/pytest` passed 42 tests; `npm --prefix viewer test` passed 19 tests; both generated-binding checks, Black check (39 files unchanged), `atlas`, `atlas impact`, and `atlas serve` help, and the production viewer build passed. Vite emitted only its existing non-blocking large-chunk warning.
- **Verified browser gate 2026-07-20:** Browser QA at 1280×720 rendered the reproducible 12-path FastAPI comparison with all 12 dependency-first review entries and editor links, 11 mapped changes, the deleted path explicitly unmapped, and 541 direct dependents. Added, modified, renamed, deleted, and dependent-risk treatments remained visually distinct and legible. System → Exceptions → Exceptions reached the changed `fastapi/exceptions.py` file level in two keyboard interactions; the third selected the file and exposed its editor link, metrics, and 40 evidence-backed dependent edges.
- **Trace/console evidence:** A compatible three-event trace rendered `fastapi/encoders.py` as edited, `fastapi/exceptions.py` and `fastapi/routing.py` as read, eight mapped changes as unobserved, and the deleted file as unmapped. The impact summary reported 8 trace-unobserved files, the browser console contained no warnings or errors, and desktop visual inspection found no clipped labels or ambiguous change/risk treatments. Phase 6 is complete.

---

## 4. How to Direct the Agent (prompting protocol)

1. **Session 1 = Phase 0 only.** Paste this whole doc, then: "Implement Phase 0 exactly. Stop at the exit criteria and show me the round-trip test output." Gate every phase the same way — agents given the whole plan at once tend to build a shallow version of everything.
2. **Give it a golden repo.** Pick one real repo (LANTERN is ideal — you can verify the map's accuracy yourself) and make it the fixture for every phase's exit test.
3. **Schemas are frozen after Phase 0.** If the agent wants a schema change mid-build, it must bump `schema_version` and update both language bindings + tests in the same commit. This prevents silent contract drift — the single biggest failure mode in agent-built multi-component systems.
4. **Demand determinism explicitly** in Phases 1–2a (fixed seeds, sorted iteration, no wall-clock or network dependence) and enforce it with the blocked-socket test in CI — `atlas analyze` must never open a connection. Same discipline as your branch-replay criterion.
5. **Budget guardrail (Phase 2b only):** every LLM call goes through one client wrapper that logs tokens/cost; hard-fail over budget. If the agent imports the provider client anywhere outside the `enrich` module, reject the commit.
6. Opus vs GPT 5.6: either can execute this; what matters is the gating. If you use Claude Code, Phase 4 is self-hosting — the agent can generate its own trace fixture while building the ingestor, which is a genuinely nice bootstrap.

---

## 5. Deferred (do not let the agent start these)
- LSP-based call graphs (jedi/pyright) for true call edges — v2.
- Multi-repo / monorepo workspace stitching.
- Git-history animation (map evolution over commits).
- GitHub-hosted PR integration (authentication, remote refs, review comments, and CI annotations).
