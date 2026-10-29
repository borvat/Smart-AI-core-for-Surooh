# Engineering project state

Smart AI Surooh core. Now contains the Surooh Memory core service v0 in `memory/` (REST + MCP, SQLite/FTS5, scoped tokens); see its README for support status.

Current source: branch `feature/surooh-memory-20261029` @ `f564918` plus uncommitted review fixes in `memory/` and this file. Original baseline: `main` @ `7d66ec8b7954f4dcf812afbb075bd01a4bed5007` (README-only at that point).

Tests: `cd memory && python -m pytest -q` passes locally (not CI). Deployment: UNVERIFIED / none. Automatic capture: NOT IMPLEMENTED (deferred). Merge to main: not done.

Next: coordinator full verification of the review fixes, then commit on the feature branch; hosting and connector registration need explicit owner approval. History lives in [HISTORY.md](HISTORY.md).

Evidence and relationships: [baseline](BASELINE.json).

## Checkpoint 2026-10-29 — Surooh Memory core v0 (local, uncommitted)

- Inspected source: branch `feature/surooh-memory-20261029` @ `44b43fabf3acfae180194ccf39fa885dc2a1ca1b`; plan [`_bmad-output/plan-surooh-memory-core.md`](../../_bmad-output/plan-surooh-memory-core.md).
- Added (untracked working tree): `memory/` — SQLite/FTS5 memory service with REST + MCP (Streamable HTTP), bearer-token scopes, secret rejection, Arabic normalization, dedup, supersession, forget tombstones, fail-loud client.
- Tested: `cd memory && python -m pytest -q` → 115 passed locally (repo `.venv` and system Python 3.13); `gitleaks dir memory --redact=100` → no leaks. Local only.
- Deployed: NONE. Committed/pushed: NO. Automatic capture: NOT IMPLEMENTED (deferred, see `_bmad-output/deferred-work.md`).
- Next: review (bmad-build step 4), then commit on the feature branch; hosting + connector registration need explicit approval.
