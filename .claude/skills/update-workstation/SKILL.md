---
name: update-workstation
description: Pull the latest main, resync both locked environments, rebuild the viewer, and verify the checkout is current. Use when the user wants their workstation updated to the newest version of ATLAS.
disable-model-invocation: true
---

Update this ATLAS checkout to the latest published state, safely.

1. **Preflight.** Run `git status --short` and `git fetch origin`. If the
   worktree has uncommitted changes, stop and show them — never pull over
   local work without the user deciding. Show `git log --oneline HEAD..origin/main`
   so the user sees what's incoming (if empty, say the checkout is already
   current and skip to step 4).
2. **Update.** `git pull --ff-only origin main`. If fast-forward fails, stop
   and report the divergence instead of merging or rebasing on your own.
3. **Resync.** `make sync` (locked uv environment, non-editable reinstall,
   plus `npm --prefix viewer ci`), then `npm --prefix viewer run build` so
   `atlas serve` serves the freshly built viewer.
4. **Verify.** `.venv/bin/atlas --help` exits cleanly and `.venv/bin/pytest`
   passes. For a deeper check offer `make check`.
5. **Report.** Summarize the commit range pulled, any dependency changes
   (`uv.lock` / `package-lock.json` in the diff), and the verification
   results.
