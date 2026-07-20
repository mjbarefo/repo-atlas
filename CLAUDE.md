# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## Claude-specific notes

- Git workflow: branch from `main` and open a pull request — do not commit
  directly to `main`.
- Run a single Python test: `.venv/bin/pytest analyzer/tests/<file>.py -k <name>`.
- Run a single viewer test: `npm --prefix viewer test -- src/<file>.test.ts`.
  Vitest only discovers tests under `viewer/` — a temporary test file must be
  placed in `viewer/src/` and removed afterward; files in scratch directories
  are never found.
