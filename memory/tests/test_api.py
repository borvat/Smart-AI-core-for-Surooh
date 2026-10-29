"""HTTP-level tests: auth, REST, MCP round-trip, startup refusal, client fail-loud."""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from surooh_memory.app import create_app
from surooh_memory.auth import ConfigError, TokenRegistry, hash_token
from surooh_memory.client import MemoryClient, MemoryRejected, MemoryUnavailable
from surooh_memory.service import MemoryService
from surooh_memory.store import Store

from .conftest import COMPANY

BASE = "http://127.0.0.1:8765"
ROOT = Path(__file__).resolve().parents[1]


def H(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def app(store, registry):
    return create_app(MemoryService(store), registry)


@pytest.fixture
async def http(app):
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as c:
            yield c


def fact(**kw):
    body = {"company_id": COMPANY, "project_id": "project-a", "text": "Ship with PostNL",
            "speaker": "user", "source_ref": "chatgpt:conv-1", "kind": "decision",
            "subject_key": "shipping.carrier"}
    body.update(kw)
    return body


# -- auth ------------------------------------------------------------------

pytestmark = pytest.mark.anyio


async def test_healthz_is_public(http):
    r = await http.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("path,method", [("/v1/facts", "GET"), ("/v1/facts", "POST"),
                                         ("/v1/export", "GET"), ("/v1/facts/" + "0" * 32, "DELETE"),
                                         ("/mcp", "POST"), ("/nope", "GET")])
@pytest.mark.parametrize("header", [None, "Bearer wrong-token-wrong-token-wrong-token", "Basic abc", "Bearer "])
async def test_every_endpoint_requires_token(http, path, method, header):
    headers = {"Authorization": header} if header else {}
    r = await http.request(method, path, headers=headers, json={})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


async def test_refuses_without_registry(store):
    with pytest.raises(TypeError):
        create_app(MemoryService(store), None)  # type: ignore[arg-type]


@pytest.mark.parametrize("env", [{}, {"SUROOH_MEMORY_TOKENS": ""}, {"SUROOH_MEMORY_TOKENS": "[]"},
                                 {"SUROOH_MEMORY_TOKENS": "not json"},
                                 {"SUROOH_MEMORY_TOKENS": json.dumps([{"principal": "x", "token": "plain",
                                                                        "scopes": ["surooh/*"]}])},
                                 {"SUROOH_MEMORY_TOKENS_FILE": "/nonexistent/file"}])
async def test_registry_refuses_bad_config(env):
    with pytest.raises(ConfigError):
        TokenRegistry.from_env(env)


async def test_server_process_refuses_to_start_without_tokens(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-m", "surooh_memory", "serve", "--db", str(tmp_path / "x.db")],
                          env=env, capture_output=True, text=True, timeout=30, cwd=tmp_path)
    assert proc.returncode == 2
    assert "refusing to start" in proc.stderr
    assert not (tmp_path / "x.db").exists()


async def test_short_token_never_authenticates():
    short = "a" * 10
    reg = TokenRegistry.from_config([{"principal": "x", "token_sha256": hash_token(short), "scopes": ["surooh/*"]}])
    assert reg.authenticate(short) is None


# -- REST matrix ---------------------------------------------------------------

async def test_rest_remember_recall_supersede(http, tokens):
    t = H(tokens["chatgpt"])
    r1 = await http.post("/v1/facts", json=fact(), headers=t)
    assert r1.status_code == 201 and r1.json()["saved"] is True
    r2 = await http.post("/v1/facts", json=fact(text="Ship with DHL"), headers=t)
    assert r2.json()["superseded_id"] == r1.json()["id"]
    dup = await http.post("/v1/facts", json=fact(text="ship with DHL!"), headers=t)
    assert dup.status_code == 200 and dup.json()["deduplicated"] and dup.json()["id"] == r2.json()["id"]
    got = (await http.get("/v1/facts", params={"company_id": COMPANY, "project_id": "project-a"}, headers=t)).json()
    assert [f["text"] for f in got["facts"]] == ["Ship with DHL"]
    assert got["facts"][0]["updated_from"]["text"] == "Ship with PostNL"
    assert got["facts"][0]["source_assistant"] == "chatgpt"


async def test_rest_secret_422(http, tokens):
    r = await http.post("/v1/facts", json=fact(text="our password: " + "Q7x" * 4), headers=H(tokens["claude"]))
    assert r.status_code == 422 and r.json()["error"] == "secret_detected" and r.json()["saved"] is False
    ex = await http.get("/v1/export", params={"company_id": COMPANY}, headers=H(tokens["claude"]))
    assert ex.json()["facts"] == []


async def test_rest_scope_403(http, tokens):
    r = await http.post("/v1/facts", json=fact(project_id="project-b"), headers=H(tokens["chatgpt"]))
    assert r.status_code == 403 and r.json()["saved"] is False
    r = await http.post("/v1/facts", json=fact(project_id="project-b"), headers=H(tokens["reader"]))
    assert r.status_code == 403
    r = await http.get("/v1/facts", params={"company_id": COMPANY, "project_id": "project-b"},
                       headers=H(tokens["chatgpt"]))
    assert r.status_code == 403


async def test_rest_cannot_spoof_source_assistant(http, tokens):
    r = await http.post("/v1/facts", json={**fact(), "source_assistant": "claude"}, headers=H(tokens["chatgpt"]))
    assert r.status_code == 422 and r.json()["saved"] is False


async def test_rest_forget(http, tokens):
    t = H(tokens["claude"])
    fid = (await http.post("/v1/facts", json=fact(), headers=t)).json()["id"]
    assert (await http.delete(f"/v1/facts/{fid}", headers=t)).json() == {"forgotten": True, "id": fid}
    assert (await http.delete(f"/v1/facts/{fid}", headers=t)).status_code == 404
    again = await http.post("/v1/facts", json=fact(), headers=t)
    assert again.status_code == 409 and again.json()["saved"] is False


async def test_rest_store_down_503(http, tokens, store):
    store.close()
    r = await http.post("/v1/facts", json=fact(), headers=H(tokens["claude"]))
    assert r.status_code == 503 and r.json()["saved"] is False
    assert (await http.get("/healthz")).status_code == 503


async def test_rest_bad_body(http, tokens):
    r = await http.post("/v1/facts", content=b"[1,2]", headers=H(tokens["claude"]))
    assert r.status_code == 400 and r.json()["saved"] is False


# -- MCP --------------------------------------------------------------------------

def mcp_client(app, token: str | None) -> Client:
    headers = H(token) if token else {}
    hc = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=headers)
    return Client(streamable_http_client(f"{BASE}/mcp", http_client=hc))


def payload(result) -> dict:
    return json.loads(result.content[0].text)


def err(result) -> dict:
    # The SDK prefixes tool errors with "Error executing tool <name>: "; the body is our JSON.
    text = result.content[0].text
    return json.loads(text[text.index("{"):])


async def test_mcp_remember_then_recall(app, tokens):
    async with app.router.lifespan_context(app):
        async with mcp_client(app, tokens["claude"]) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"remember", "recall", "forget"} <= names
            r = await c.call_tool("remember", {"company_id": COMPANY, "project_id": "project-a",
                                               "text": "Bol.com returns go to Borvat warehouse", "speaker": "user",
                                               "source_ref": "claude:conv-9", "subject_key": "returns.address",
                                               "kind": "decision"})
            assert not r.is_error
            saved = payload(r)
            assert saved["saved"] is True
            r = await c.call_tool("recall", {"company_id": COMPANY, "project_id": "project-a",
                                             "query": "returns warehouse"})
            facts = payload(r)["facts"]
            assert [f["id"] for f in facts] == [saved["id"]]
            assert facts[0]["source_assistant"] == "claude"


async def test_mcp_errors_report_not_saved(app, tokens):
    async with app.router.lifespan_context(app):
        async with mcp_client(app, tokens["chatgpt"]) as c:
            r = await c.call_tool("remember", {"company_id": COMPANY, "project_id": "project-b",
                                               "text": "x fact", "source_ref": "r", "speaker": "user"})
            assert r.is_error and err(r) == {
                "error": "forbidden", "detail": "token has no write access to this scope", "saved": False}
            r = await c.call_tool("remember", {"company_id": COMPANY, "project_id": "project-a",
                                               "text": "token=" + "Ab1" * 10, "source_ref": "r",
                                               "speaker": "user"})
            assert r.is_error and err(r)["error"] == "secret_detected" and err(r)["saved"] is False


async def test_mcp_without_token_is_401(app):
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as hc:
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}}}
            r = await hc.post("/mcp", json=init,
                              headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 401


# -- client ---------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_client_server_down_raises():
    with MemoryClient(f"http://127.0.0.1:{_free_port()}", "t" * 40, timeout=2) as c:
        with pytest.raises(MemoryUnavailable):
            c.remember(company_id=COMPANY, project_id="project-a", text="x", source_ref="r", speaker="user")
        with pytest.raises(MemoryUnavailable):
            c.recall(company_id=COMPANY)


def test_client_never_reports_unconfirmed_save():
    def handler(request):
        return httpx2.Response(200, json={"saved": False})
    with MemoryClient(BASE, "t" * 40, transport=httpx2.MockTransport(handler)) as c:
        with pytest.raises(MemoryUnavailable):
            c.remember(company_id=COMPANY, project_id="project-a", text="x", source_ref="r", speaker="user")

    def html(request):
        return httpx2.Response(502, text="<html>bad gateway</html>")
    with MemoryClient(BASE, "t" * 40, transport=httpx2.MockTransport(html)) as c:
        with pytest.raises(MemoryUnavailable):
            c.remember(company_id=COMPANY, project_id="project-a", text="x", source_ref="r", speaker="user")


def test_client_round_trip_live_server(tmp_path, tokens, token_env):
    """Real uvicorn process: saves, restart on the same DB, recall still returns the fact."""
    port = _free_port()
    db = tmp_path / "live.db"
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT), "SUROOH_MEMORY_TOKENS": token_env}

    def start():
        p = subprocess.Popen([sys.executable, "-m", "surooh_memory", "serve", "--db", str(db),
                              "--port", str(port)], env=env, cwd=tmp_path,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        import time
        for _ in range(100):
            try:
                if httpx2.get(f"http://127.0.0.1:{port}/healthz", trust_env=False).status_code == 200:
                    return p
            except httpx2.HTTPError:
                time.sleep(0.1)
        p.kill()
        raise AssertionError(p.stdout.read().decode() if p.stdout else "server did not start")

    p = start()
    try:
        with MemoryClient(f"http://127.0.0.1:{port}", tokens["chatgpt"]) as c:
            saved = c.remember(company_id=COMPANY, project_id="project-a", text="VAT number lives in Exact",
                               source_ref="chatgpt:conv-3", speaker="user")
            with pytest.raises(MemoryRejected) as ei:
                c.remember(company_id=COMPANY, project_id="project-b", text="x", source_ref="r", speaker="user")
            assert ei.value.status == 403
    finally:
        p.terminate()
        p.wait(timeout=10)

    with MemoryClient(f"http://127.0.0.1:{port}", tokens["chatgpt"], timeout=2) as c:
        with pytest.raises(MemoryUnavailable):
            c.remember(company_id=COMPANY, project_id="project-a", text="while down", source_ref="r", speaker="user")

    p = start()
    try:
        with MemoryClient(f"http://127.0.0.1:{port}", tokens["chatgpt"]) as c:
            facts = c.recall(company_id=COMPANY, project_id="project-a")
            assert [f["id"] for f in facts] == [saved["id"]]
    finally:
        p.terminate()
        p.wait(timeout=10)
    # sanity: the DB file is real and holds the row
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


async def test_mcp_speaker_is_required(app, tokens):
    async with app.router.lifespan_context(app):
        async with mcp_client(app, tokens["claude"]) as c:
            r = await c.call_tool("remember", {"company_id": COMPANY, "project_id": "project-a",
                                               "text": "no speaker given", "source_ref": "r"})
            assert r.is_error
            r = await c.call_tool("recall", {"company_id": COMPANY, "project_id": "project-a"})
            assert payload(r)["facts"] == []


async def test_rest_oversized_body_413(http, tokens):
    big = b'{"text": "' + b"x" * (70 * 1024) + b'"}'
    r = await http.post("/v1/facts", content=big, headers={**H(tokens["claude"]), "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["saved"] is False


async def test_non_http_scope_is_not_passed_through(app):
    called = []

    async def inner(scope, receive, send):
        called.append(scope["type"])

    from surooh_memory.app import BearerAuth
    sent = []

    async def send(msg):
        sent.append(msg)

    mw = BearerAuth(inner, app.user_middleware[0].kwargs["registry"])
    await mw({"type": "websocket", "path": "/mcp", "headers": []}, None, send)
    assert called == [] and sent == [{"type": "websocket.close", "code": 1008}]
