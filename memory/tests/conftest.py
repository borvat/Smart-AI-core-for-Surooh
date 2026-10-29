from __future__ import annotations

import json
import secrets

import pytest

from surooh_memory.auth import Principal, TokenRegistry, hash_token
from surooh_memory.service import MemoryService
from surooh_memory.store import Store

COMPANY = "surooh"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_principal(name: str, scopes: list[str], perms=("read", "write")) -> Principal:
    parsed = frozenset(tuple(s.split("/")) for s in scopes)
    return Principal(name=name, scopes=parsed, permissions=frozenset(perms))


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "mem.db"))
    yield s
    s.close()


@pytest.fixture
def service(store) -> MemoryService:
    return MemoryService(store)


@pytest.fixture
def claude() -> Principal:
    return make_principal("claude", [f"{COMPANY}/*"])


@pytest.fixture
def tokens() -> dict[str, str]:
    # Generated per run; never written to disk in plaintext.
    return {
        "claude": secrets.token_urlsafe(32),
        "chatgpt": secrets.token_urlsafe(32),
        "reader": secrets.token_urlsafe(32),
    }


@pytest.fixture
def token_config(tokens) -> list[dict]:
    return [
        {"principal": "claude", "token_sha256": hash_token(tokens["claude"]),
         "scopes": [f"{COMPANY}/*"], "permissions": ["read", "write"]},
        {"principal": "chatgpt", "token_sha256": hash_token(tokens["chatgpt"]),
         "scopes": [f"{COMPANY}/project-a"], "permissions": ["read", "write"]},
        {"principal": "reader", "token_sha256": hash_token(tokens["reader"]),
         "scopes": [f"{COMPANY}/project-b"], "permissions": ["read"]},
    ]


@pytest.fixture
def registry(token_config) -> TokenRegistry:
    return TokenRegistry.from_config(token_config)


@pytest.fixture
def token_env(token_config) -> str:
    return json.dumps(token_config)
