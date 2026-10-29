"""Bearer-token authentication and scope checks.

Tokens are configured only as SHA-256 hashes, via the ``SUROOH_MEMORY_TOKENS``
environment variable (JSON) or a file named by ``SUROOH_MEMORY_TOKENS_FILE``::

    [
      {"principal": "claude",  "token_sha256": "<64 hex>",
       "scopes": ["surooh/*"], "permissions": ["read", "write"]},
      {"principal": "chatgpt", "token_sha256": "<64 hex>",
       "scopes": ["surooh/shipping", "surooh/catalog"], "permissions": ["read", "write"]}
    ]

A scope is ``<company_id>/<project_id>`` or ``<company_id>/*``. The principal
name is recorded as ``source_assistant`` on every write, so one assistant
cannot attribute a fact to another.

Plaintext tokens are never configured, logged or stored. Use
``python -m surooh_memory hash-token`` (reads the token from stdin) to make a hash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field

ENV_TOKENS = "SUROOH_MEMORY_TOKENS"
ENV_TOKENS_FILE = "SUROOH_MEMORY_TOKENS_FILE"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PERMISSIONS = {"read", "write"}
MIN_TOKEN_LENGTH = 32


class ConfigError(RuntimeError):
    """Raised when the service is not safely configured; the server must not start."""


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[tuple[str, str]]  # (company_id, project_id or "*")
    permissions: frozenset[str] = field(default_factory=frozenset)

    def allows(self, permission: str, company_id: str, project_id: str) -> bool:
        if permission not in self.permissions:
            return False
        return (company_id, project_id) in self.scopes or (company_id, "*") in self.scopes

    def readable_projects(self, company_id: str) -> list[str] | None:
        """``None`` = every project of the company; otherwise the explicit list (may be empty)."""
        if "read" not in self.permissions:
            return []
        if (company_id, "*") in self.scopes:
            return None
        return sorted(p for c, p in self.scopes if c == company_id)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_scope(raw: str) -> tuple[str, str]:
    if not isinstance(raw, str) or raw.count("/") != 1:
        raise ConfigError(f"invalid scope {raw!r}: expected 'company/project' or 'company/*'")
    company, project = raw.split("/")
    if not _ID_RE.match(company) or not (project == "*" or _ID_RE.match(project)):
        raise ConfigError(f"invalid scope {raw!r}")
    return company, project


class TokenRegistry:
    def __init__(self, entries: list[tuple[bytes, Principal]]) -> None:
        if not entries:
            raise ConfigError("no API tokens configured; refusing to start")
        self._entries = entries

    @classmethod
    def from_config(cls, data: object) -> "TokenRegistry":
        if not isinstance(data, list) or not data:
            raise ConfigError("token config must be a non-empty JSON list")
        entries: list[tuple[bytes, Principal]] = []
        seen_hashes: set[str] = set()
        seen_names: set[str] = set()
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise ConfigError(f"token entry {i} must be an object")
            name = item.get("principal")
            digest = str(item.get("token_sha256", "")).lower()
            scopes = item.get("scopes")
            perms = item.get("permissions", ["read", "write"])
            if not isinstance(name, str) or not _ID_RE.match(name):
                raise ConfigError(f"token entry {i}: invalid principal name")
            if "token" in item:
                raise ConfigError(f"token entry {i}: plaintext 'token' is not accepted; use token_sha256")
            if not _HEX64.match(digest):
                raise ConfigError(f"token entry {i}: token_sha256 must be 64 hex chars")
            if not isinstance(scopes, list) or not scopes:
                raise ConfigError(f"token entry {i}: scopes must be a non-empty list")
            if not isinstance(perms, list) or not perms or not set(perms) <= _PERMISSIONS:
                raise ConfigError(f"token entry {i}: permissions must be a subset of {sorted(_PERMISSIONS)}")
            if digest in seen_hashes:
                raise ConfigError(f"token entry {i}: duplicate token hash")
            if name in seen_names:
                raise ConfigError(f"token entry {i}: duplicate principal {name!r}")
            seen_hashes.add(digest)
            seen_names.add(name)
            principal = Principal(
                name=name,
                scopes=frozenset(_parse_scope(s) for s in scopes),
                permissions=frozenset(perms),
            )
            entries.append((bytes.fromhex(digest), principal))
        return cls(entries)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "TokenRegistry":
        env = os.environ if environ is None else environ
        raw = env.get(ENV_TOKENS)
        path = env.get(ENV_TOKENS_FILE)
        if raw and path:
            raise ConfigError(f"set only one of {ENV_TOKENS} or {ENV_TOKENS_FILE}")
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError as exc:
                raise ConfigError(f"cannot read {ENV_TOKENS_FILE}: {exc.strerror}") from None
        if not raw or not raw.strip():
            raise ConfigError(f"no API tokens configured ({ENV_TOKENS} / {ENV_TOKENS_FILE}); refusing to start")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ConfigError("token config is not valid JSON") from None
        return cls.from_config(data)

    def authenticate(self, token: str | None) -> Principal | None:
        """Constant-time lookup: every configured hash is compared, no early exit."""
        token = token or ""
        presented = hashlib.sha256(token.encode("utf-8")).digest()
        match: Principal | None = None
        for digest, principal in self._entries:
            if hmac.compare_digest(presented, digest):
                match = principal
        if not MIN_TOKEN_LENGTH <= len(token) <= 512:
            return None
        return match


def bearer_from_header(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
