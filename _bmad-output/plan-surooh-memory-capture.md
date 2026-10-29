---
title: 'Surooh Memory: owner-protected forget, audit log, Claude Code automatic capture and recall'
type: 'feature'
ticket: ''
created: '2026-10-29'
status: 'in-progress'
baseline_revision: '7ea1d52'
route: 'full'
route_source: 'auto'
risk: 'high'
review: ''
review_source: ''
lenses_ran: []
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/plan-surooh-memory-core.md'
  - '{project-root}/memory/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The memory core exists but nothing is saved or recalled unless an assistant is explicitly told to; owner decisions can be erased by any writer and changes leave no audit trail.

**Approach:** Add capability-based permissions (owner-only deletion of owner decisions), an append-only audit log without deleted text, and a Claude Code integration — lifecycle hooks plus a pluggable transcript extractor — that saves decisions and injects relevant context automatically, proven with fresh headless Claude Code sessions.

## Boundaries & Constraints

**Always:**
- Token capabilities: `read`, `write` (assistant suggestions), `write_user` (user-stated facts), `forget_own` (only rows the same principal wrote as `assistant_suggestion`), `forget_any` (owner). A `speaker=user` fact can be forgotten only with `forget_any`.
- Audit log records every remember/supersede/promote/forget and every denied write or forget: time, principal, action, fact id, company, project, content_hash, outcome. Never the text. Owner-only read endpoint.
- Owner-only endpoint to verify a text is forgotten (by hash) so re-save prevention is checkable.
- Hooks: `SessionStart` injects active decisions for the configured company/project; `UserPromptSubmit` injects facts relevant to the prompt; `Stop` and `SessionEnd` hand new transcript turns to a detached extractor worker and return immediately.
- Extractor output: candidate facts with text, kind, speaker (`user` only when the user stated or explicitly approved it), subject_key, all passed through the existing write pipeline.
- Recursion guard: an extractor that runs Claude Code must not trigger the hooks again.
- Failures are visible: the next hook reports the previous capture's outcome (saved N / failed / pending), and an unreachable server is reported as such; nothing claims a save the server did not confirm. Unsent facts go to a local outbox retried on the next hook.
- Scope comes from a project config file; a session never reads or writes outside its configured company/project.
- Tokens are read from an env var or a 0600 file, never from repo files.

**Never:** deploying, paid services, merging to main, rebuilding or re-testing the core beyond what these changes touch, storing transcripts on the server, claiming automatic behaviour for any platform other than Claude Code.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Assistant forgets owner decision | write token, speaker=user fact | denied, fact intact, audit `forget_denied` | 403 |
| Assistant forgets own suggestion | forget_own token | erased, audit `forget` | — |
| Owner forgets decision | forget_any token | erased everywhere, tombstone, audit row has no text | — |
| Verify forgotten | owner, same text | `forgotten: true` | 403 for non-owner |
| Write user fact without write_user | speaker=user | denied | 403, audit |
| New session start | facts in scope | context injected | server down → visible notice, no crash |
| Prompt submitted | related facts exist | relevant facts injected | — |
| Turn ends with decision | transcript | worker saves, next hook reports saved N | failure → outbox + visible notice |
| Extractor's own session | guard env set | hooks exit immediately | — |
| Other project config | facts in project A | nothing from A injected in B | — |

</frozen-after-approval>

## Code Map

- Core service: `memory/surooh_memory/{auth,service,store,app,client}.py` — extend, do not rewrite. Current auth scopes carry read/write; add capabilities there and enforce in `service.py`. Store SQL stays in `store.py` (add `audit` table, keep tombstones).
- Existing tests in `memory/tests/` must keep passing; add tests, do not duplicate passing ones.
- Claude Code hook contract (CLI 2.1.312 present): hook receives JSON on stdin (`session_id`, `transcript_path`, `cwd`, `hook_event_name`, `prompt` for UserPromptSubmit); SessionStart/UserPromptSubmit may print `{"hookSpecificOutput":{"hookEventName":...,"additionalContext":...}}`; any hook may print `{"systemMessage": ...}` to show the user a notice. Transcript is JSONL. Verify field names against Context7 / official docs before relying on them.
- Headless Claude Code works here: `claude -p ... --output-format json` (unset `CLAUDECODE` and session env vars for a nested run). Each run costs plan usage — keep end-to-end runs minimal.

## Tasks & Acceptance

**Execution:**
- [ ] `memory/surooh_memory/auth.py` -- capabilities per token, backward-compatible config -- permissions
- [ ] `memory/surooh_memory/store.py` -- audit table + queries -- audit trail
- [ ] `memory/surooh_memory/service.py` -- enforce capabilities, write audit rows, forgotten-check -- rules
- [ ] `memory/surooh_memory/app.py` -- `GET /v1/audit`, `POST /v1/forgotten/check` (owner) -- API
- [ ] `memory/surooh_memory/extractor.py` -- Extractor protocol, ClaudeCodeExtractor (headless `claude -p`, small model, no tools, JSON), StubExtractor -- extraction
- [ ] `memory/surooh_memory/hooks/` -- hook entrypoints, scope config, state/outbox, detached worker, installer writing `.claude/settings.json` -- Claude Code integration
- [ ] `memory/tests/` -- matrix rows + hook/worker unit tests with stub extractor and fake transcripts -- proof
- [ ] `memory/scripts/e2e_claude_code.sh` -- end-to-end acceptance with real fresh headless Claude Code sessions against a local server and a synthetic test scope -- proof
- [ ] `memory/README.md` -- permissions, audit, hooks setup, data sent to model providers, cost -- docs

**Acceptance Criteria:**
- Given a fresh headless Claude Code session in a configured project, when the user states a new decision, then after the session ends the decision exists in memory as `speaker=user` without any save instruction.
- Given a later fresh session with no tools allowed, when asked a question that needs that decision, then the answer contains it, coming only from injected context.
- Given an updated decision in a later session, then the next fresh session answers with the newer decision.
- Given a session configured for a different project, then the decision is not injected.
- Given the owner forgets the decision, then a fresh session does not get it and re-saving it is refused and verifiable.
- Given the server is down, then the session continues, a visible notice says memory is unavailable, and nothing is reported saved; after restart the outbox is delivered.

## Implementation Notes

## Plan Change Log

## Review Triage Log

## Verification

**Commands:**
- `cd memory && python -m pytest -q` -- expected: all pass
- `bash memory/scripts/e2e_claude_code.sh` -- expected: every acceptance step PASS with evidence written to `memory/evidence/`
- `gitleaks dir memory --redact=100 --no-banner` -- expected: no leaks
