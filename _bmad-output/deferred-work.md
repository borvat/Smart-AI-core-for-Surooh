- source_plan: none
  summary: Claude Code lifecycle hooks (SessionStart/UserPromptSubmit inject, Stop capture) plus pluggable transcript extractor for Surooh Memory.
  evidence: Split from the 2026-10-29 Surooh Memory intent; independently shippable on top of the memory core API, which must exist first.
- source_plan: `_bmad-output/plan-surooh-memory-core.md`
  summary: Decide who may forget a user decision (owner-only token vs any write token).
  evidence: Review finding 8 — any write-scoped token can erase and tombstone a speaker=user decision; frozen intent does not settle it.
- source_plan: `_bmad-output/plan-surooh-memory-core.md`
  summary: Per-token permission for writing speaker=user facts.
  evidence: speaker is caller-declared; a token holder can claim a suggestion is a user decision.
