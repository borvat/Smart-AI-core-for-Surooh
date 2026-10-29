# Surooh Memory — core service v0

One shared memory that ChatGPT, Claude and later SAM/Hermes all read and write, so a
decision made in one assistant is not lost in another. There is **one** store; there is
no per-assistant memory.

## Support status (be precise)

| Capability | Status |
|---|---|
| REST API (`/v1/...`) | Implemented, tested locally (in-process and real uvicorn process) |
| MCP over Streamable HTTP (`/mcp`, tools `remember` / `recall` / `forget`) | Implemented, tested locally with the `mcp` 2.3.0 client |
| Bearer-token auth, scopes, read/write permissions | Implemented, tested |
| Secret rejection, Arabic/Latin normalization, dedup, supersession, forget | Implemented, tested |
| Restart persistence (SQLite file) | Tested |
| **Automatic capture** (hooks, transcript extractor, LLM extraction) | **Not implemented.** Deferred to a separate goal. Nothing is saved unless an assistant or client explicitly calls `remember`. |
| Gmail ingestion | Not implemented, out of scope |
| Deployment / hosting | Not done. No deploy, no paid service. Local only. |
| ChatGPT / Claude connector registration | Not done. Requires a reachable HTTPS host first. |
| Postgres backend | Not implemented. All SQL is isolated in `store.py` for a later adapter. |

## Architecture

```
assistant (ChatGPT / Claude / SAM)            future capture hook
        │  MCP tools                                   │ surooh_memory.client (fail-loud)
        ▼                                              ▼
   /mcp (Streamable HTTP, stateless)            /v1/facts, /v1/export
        └──────────────┬───────────────────────────────┘
                 BearerAuth middleware  (everything except /healthz)
                       │  token → Principal{name, scopes, permissions}
                       ▼
                 service.MemoryService   (business rules)
                       ▼
                 store.Store             (SQLite + FTS5; only module with SQL)
```

| Module | Role |
|---|---|
| `normalize.py` | NFKC, Arabic letter folding (أإآ→ا, ى→ي, ة→ه, ؤ→و, ئ→ي), harakat/tatweel removal, Arabic-Indic digits, casefold, punctuation removal, light `ال`/`وال`/`بال`/`فال`/`كال`/`لل` prefix stripping, SHA-256 content hash |
| `secrets_filter.py` | Credential detectors: known key formats, `password/token/api_key/كلمة السر/wachtwoord … = value`, URL credentials, JWT, bearer headers, private keys, IBAN, Luhn-valid card numbers, high-entropy strings |
| `store.py` | Schema, FTS5 index, repository methods, secure erase |
| `service.py` | Write pipeline, recall, forget, export |
| `auth.py` | Token registry from env (hashes only), constant-time lookup, scope checks |
| `app.py` | Starlette REST + MCP mount behind auth |
| `client.py` | Fail-loud REST client for future hooks |

### Fact model

Every fact carries `company_id`, `project_id`, `source_assistant`, `source_ref`,
`created_at`, `speaker` (`user` | `assistant_suggestion`), plus `id`, `kind`
(`decision|fact|preference|instruction|note`), optional `subject_key`
(e.g. `shipping.carrier`), `status` (`active|proposed|superseded`), `supersedes`,
`superseded_by`, `updated_at`.

`source_assistant` is **always the authenticated principal name**, never a value the
caller supplies, so one assistant cannot attribute a fact to another.

### Write pipeline (`remember`)

1. Validate fields (ids `^[a-z0-9][a-z0-9_-]{0,63}$`, text ≤ 4000 chars, `source_ref` required).
2. Scope check — principal needs `write` on `company/project` → else **403**.
3. Secret check on `text`, `subject_key`, `source_ref` → **422 `secret_detected`**, nothing stored, value never echoed.
4. Normalize + hash.
5. Tombstone check — forgotten content → **409 `forgotten`**.
6. Dedup — same normalized text in the same scope returns the existing id (`deduplicated: true`, no new row).
   If the existing row is an assistant suggestion and the user now states it, that row is promoted to an active user decision.
7. Supersession — a `user` fact with a `subject_key` replaces the active fact on that subject in that scope (old → `superseded`, `new.supersedes = old.id`).
   An `assistant_suggestion` is stored as `proposed` and **never** supersedes anything.

Any store error → **503** with `saved: false`. Every failed write response carries `saved: false`.

### Recall

Scoped to what the token may read. Returns active + proposed facts (best FTS match, or newest
first), approved user facts before suggestions. Superseded facts are never returned directly;
the latest active fact per subject includes `updated_from` (previous text, its `created_at`
and source) so a newer decision is never confused with an older one. Proposed facts carry
`approved: false` and `label: "UNAPPROVED assistant suggestion - not a user decision"`.

### Forget

`DELETE /v1/facts/{id}` / MCP `forget`: the row and its FTS entry are deleted with SQLite
`secure_delete` (and FTS5 `secure-delete`), the WAL is checkpointed, and only a
`(company, project, content_hash)` tombstone remains. Re-saving the same normalized text in
that scope is refused, so background capture cannot silently re-create forgotten content.
Unknown ids **and ids outside the caller's scope** both return **404** (no existence oracle).

## API

All endpoints except `GET /healthz` require `Authorization: Bearer <token>`; otherwise **401**.

| Method | Path | Body / query |
|---|---|---|
| GET | `/healthz` | — (checks DB) |
| POST | `/v1/facts` | `company_id, project_id, text, speaker, source_ref, kind?, subject_key?` → 201 new / 200 dedup |
| GET | `/v1/facts` | `company_id, project_id?, query?, limit? (1–100)` |
| DELETE | `/v1/facts/{id}` | — |
| GET | `/v1/export` | `company_id, project_id?` — all facts in scope (incl. superseded) + forgotten count |
| POST | `/mcp` | MCP Streamable HTTP (stateless, JSON responses). Tools: `remember`, `recall`, `forget`. `speaker` is required on `remember`. |

## Configuration and running (local only)

```bash
cd memory
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'

# 1. make a token and its hash; the token stays in a shell variable (no file, not on a command line)
TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s\n' "$TOKEN" | python -m surooh_memory hash-token   # prints the sha256 for the config
# hand "$TOKEN" to the assistant's own secret store, then: unset TOKEN

# 2. configure principals with hashes only
export SUROOH_MEMORY_TOKENS='[{"principal":"claude","token_sha256":"<hash>","scopes":["surooh/*"],"permissions":["read","write"]}]'
# or SUROOH_MEMORY_TOKENS_FILE=/path/to/tokens.json

python -m surooh_memory serve --db ./surooh_memory.db --host 127.0.0.1 --port 8765
```

Scopes are `company/project` or `company/*`. The server **exits with code 2 and does not
create a DB** if no valid token configuration is present. Plaintext `token` fields in the
config are rejected. Tokens must be ≥ 32 characters.

`SUROOH_MEMORY_ALLOWED_HOSTS` (comma list, default localhost only) sets the Host values
accepted on `/mcp` (DNS-rebinding protection). Set it to the public host name when this is
ever put behind a reverse proxy.

## Tests

```bash
cd memory && python -m pytest -q
gitleaks dir memory --redact=100 --no-banner
```

Covers every row of the plan's I/O matrix, restart persistence, project isolation, MCP
round-trip with token and 401 without, server refusal without tokens, and the client raising
`MemoryUnavailable` when the server is down or does not confirm a save. Credential-shaped test
inputs are generated at runtime, so the repository itself contains no secret-like strings.

## Threat model

| Threat | Mitigation | Residual risk |
|---|---|---|
| Unauthenticated access | Auth middleware wraps REST and MCP; only `/healthz` is public; server refuses to start without tokens | — |
| Token theft from config/DB/logs | Only SHA-256 hashes configured; tokens never stored or logged | Plain SHA-256 is fine for 32+ char random tokens, not for human passwords — tokens must be random |
| Timing attack on tokens | Fixed-length digest compare with `hmac.compare_digest` against every entry | — |
| Cross-project / cross-company leakage | Every read/write/forget is checked against the principal's scopes in the service layer; recall SQL is filtered by allowed projects | A `company/*` token reads every project of that company — issue narrow scopes to less-trusted assistants |
| Assistant impersonation | `source_assistant` = authenticated principal, request field rejected | Two assistants sharing one token cannot be told apart — issue one token per assistant |
| Assistant overriding the user | Suggestions are `proposed`, labelled unapproved, never supersede | An assistant that falsely sends `speaker=user` is trusted; `speaker` is a client claim. Mitigation later: per-token "may write user decisions" permission |
| Credentials saved into memory | Detector rejects before storage; values never echoed | Pattern-based: novel formats or a password written as a plain word may pass. False positives possible (e.g. long random IDs in text) |
| Forgotten data resurfacing | Row + FTS row deleted with secure_delete, WAL checkpoint, hash tombstone blocks re-save | Backups/snapshots of the DB file taken before forget still hold the text; tombstone matches only the same normalized text in the same scope |
| FTS query injection | Query tokens are quoted, FTS operators neutralized | — |
| DNS rebinding against a local MCP endpoint | MCP transport Host/Origin allow-list (localhost by default) | — |
| Large-body DoS | 64 KiB body limit on REST and MCP | No rate limiting yet |
| Store failure reported as success | Store errors → 503 `saved:false`; client raises unless `saved: true` | — |

## Known limitations

- Single-process SQLite; one writer at a time. Fine for a few assistants, not for high volume.
- Arabic matching is normalization + light prefix stripping, not full morphological stemming
  (e.g. `الشحن` matches `شحن`, but not `نشحن`). Search is lexical (FTS5/BM25); no embeddings.
- No audit log of reads; no rate limiting; no token rotation tooling beyond editing config.
