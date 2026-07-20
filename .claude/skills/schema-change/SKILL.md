---
name: schema-change
description: Workflow for changing the artifact schemas in shared/schemas — regenerate both language bindings, update fixtures, and run contract tests. Use whenever map.schema.json, trace.schema.json, or impact.schema.json changes, or when a task requires new artifact fields.
---

The JSON Schemas in `shared/schemas/` are the source of truth for the map,
trace, and impact artifacts. Both language bindings are generated; drift
between them is the known failure mode. Never hand-edit
`analyzer/src/atlas_analyzer/models/{map,trace,impact}.py` or
`viewer/src/generated/`.

Follow this order:

1. Edit the schema(s) in `shared/schemas/`. Keep `schema_version` at its
   current value for backward-compatible additions; bump it only for breaking
   changes and say so explicitly in the PR.
2. Regenerate the Python models: `.venv/bin/python scripts/generate_models.py`
   (then `--check` must pass).
3. Regenerate the TypeScript types: `npm --prefix viewer run generate:types`
   (then `npm --prefix viewer run check:generated` must pass).
4. Update the cross-language examples in `shared/fixtures/` to exercise any
   new fields.
5. Run the contract tests in both languages:
   `.venv/bin/pytest analyzer/tests/test_artifact_contracts.py` and
   `npm --prefix viewer test -- src/contracts.test.ts`.
6. Finish with `make check` — schema changes ripple into the analyzer,
   viewer, and validation scripts, and only the full gate proves nothing
   drifted.
