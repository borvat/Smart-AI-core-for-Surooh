---
title: 'Surooh Memory core service v0'
type: 'feature'
ticket: ''
created: '2026-10-29'
status: 'in-review'
baseline_revision: '3a1133105eb14ff58522b844b7ad65f227b790e3'
route: 'full'
route_source: 'auto'
risk: 'high'
review: 'quick'
review_source: 'pinned'
lenses_ran: ['quick']
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Surooh has no shared memory that ChatGPT, Claude and later SAM/Hermes can all read and write, so decisions stated in one assistant are lost in another.

**Approach:** One authenticated memory service (module `memory/`) holding scoped, provenance-tagged facts with a write pipeline (secret rejection, Arabic normalization, dedup, supersession) and scoped retrieval, exposed over MCP (Streamable HTTP) and REST. Capture hooks and the LLM extractor are a separate deferred goal.

## Boundaries & Constraints

**Always:** every fact carries company_id, project_id, source_assistant, source_ref, created_at, speaker; assistant suggestions never supersede user decisions; every endpoint except `/healthz` requires a bearer token mapped to allowed scopes; the server refuses to start without configured tokens; a failed write reports `saved: false`; forgotten content is erased and its hash blocks re-capture.

**Never:** deploy, paid services, merge to main, storing secrets/passwords/keys/tokens, a per-assistant store, Gmail ingestion, claiming automatic capture.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| New decision | user decision, subject_key `shipping.carrier` | stored active | — |
| Update decision | newer user decision, same subject_key+scope | old → superseded, new.supersedes = old | — |
| Assistant suggestion | speaker=assistant_suggestion | status proposed, labeled unapproved in recall | — |
| Exact/near duplicate | same normalized text in scope | no new row, returns existing id | — |
| Secret | text with key/password/token | rejected, nothing stored | 422 `secret_detected` |
| Out-of-scope write/read | token lacks scope | rejected / filtered | 403 |
| Forget | id | text erased, never recalled, re-add blocked | 404 if unknown |
| Store down | DB error | `saved:false` | 503 |
| Client, server down | connection refused | raises `MemoryUnavailable` | never reports saved |

</frozen-after-approval>

## Code Map

- Repo is README-only (baseline 3a11331); no code to reuse. `docs/engineering-memory/*` is repo engineering memory — do not change except state/history at boundary.
- `mcp` 2.3.0: `from mcp.server import MCPServer`; `mcp.streamable_http_app(stateless_http=True)`; host app must enter `mcp.session_manager.run()` in its lifespan; tests use `mcp.client.Client(server)`.

## Tasks & Acceptance

**Execution:**
- [x] `memory/pyproject.toml` -- package + deps -- reproducible env
- [x] `memory/surooh_memory/normalize.py` -- Arabic/Latin normalization, light al- prefix stripping, content hash -- dedup + search
- [x] `memory/surooh_memory/secrets_filter.py` -- secret detectors -- never store credentials
- [x] `memory/surooh_memory/store.py` -- SQLite schema, FTS5, repository -- persistence (SQL isolated for later Postgres adapter)
- [x] `memory/surooh_memory/service.py` -- write pipeline, recall, forget, export -- business rules
- [x] `memory/surooh_memory/auth.py` -- token→principal scopes from env, hashed, constant-time -- access control
- [x] `memory/surooh_memory/app.py` -- Starlette REST + MCP mount behind auth -- API
- [x] `memory/surooh_memory/client.py` -- fail-loud client -- for future hooks
- [x] `memory/tests/` -- matrix + acceptance 4–7 -- proof
- [x] `memory/README.md` -- architecture, threat model, support status -- documentation

**Acceptance Criteria:**
- Given a running service restarted on the same DB, when recalling, then previously saved facts return.
- Given two projects, when recalling with project A scope, then no project B fact appears.
- Given an MCP client with a valid token, when calling `remember` then `recall`, then the fact round-trips; without a token the MCP endpoint returns 401.

## Implementation Notes

- MCP tool errors arrive as `Error executing tool <name>: {json}` (SDK prefix); the JSON body carries `saved: false` on failed writes.
- MCP `remember` requires `speaker` (no default), so an assistant must declare user vs suggestion explicitly.
- `source_assistant` is always the authenticated principal name; a REST body field of that name is rejected (422).
- Forget/out-of-scope ids both return 404 (no existence oracle). Forgotten-content re-add returns 409 `forgotten`.
- Card-number detector requires 15–19 Luhn-valid digits so 13/14-digit EAN/GTIN codes are not rejected.
- A user restating an assistant suggestion promotes that row to an active user decision instead of creating a duplicate.

## Plan Change Log

## Review Triage Log

## Design Notes

Forget keeps only a hash tombstone so background capture cannot silently re-save forgotten content. Recall returns the latest active fact per subject_key plus `updated_from` (previous text date) so a newer decision is never confused with an older one.

## Verification

**Commands:**
- `cd memory && python -m pytest -q` -- expected: all pass
- `gitleaks dir memory --redact=100 --no-banner` -- expected: no leaks
