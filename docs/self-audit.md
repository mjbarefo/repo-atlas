# ATLAS Self-Audit

This is the first map-quality baseline for ATLAS itself. It is intentionally a
living snapshot: when a finding is fixed, update the expectation only after the
new map boundary has been inspected.

Run the audit from the source checkout:

```bash
.venv/bin/atlas analyze . --output /tmp/atlas-self-audit-map.json
.venv/bin/atlas audit --map /tmp/atlas-self-audit-map.json
.venv/bin/python scripts/validate_self_audit.py
```

## Ruleset 1 baseline

The baseline records four evidence-backed findings:

- `large-module`: **Atlas Analyzer** groups 22 source files and 4,196 LOC into
  one module. The boundary hides likely submodules such as analysis,
  abstraction, artifact I/O, serving, ingestion, and impact.
- `module-cycle`: **Atlas Analyzer** and **Enrichment** import each other. The
  evidence is the enrichment implementation's dependency on core models and
  the core CLI's dependency on enrichment; the coarse core grouping makes that
  architectural relationship look cyclic.
- `large-file`: `viewer/src/App.tsx` contains 1,162 LOC and several distinct
  concerns: artifact loading, navigation, layout, canvas interaction, detail
  inspection, trace state, and impact state.
- `thin-module`: `viewer/vite.config.ts` becomes its own 7-LOC module. It is
  useful repository context but probably not a meaningful architecture layer.

The expected finding identities live in
`analyzer/tests/fixtures/self_audit_expectations.json`. Threshold behavior is
covered by focused analyzer tests; the validation script proves the current
repository still matches this explicit baseline.

## Interpretation

Audit findings are review prompts, not automatic declarations that the code is
wrong. Every finding includes the node IDs and measured facts that triggered
it. Ruleset 1 deliberately avoids speculative scores and unresolved-import
claims because the v1 artifact does not yet retain enough evidence for either.
