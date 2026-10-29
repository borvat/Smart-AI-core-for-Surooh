"""HTTP surface: REST under ``/v1`` and MCP (Streamable HTTP) at ``/mcp``.

Every path except ``/healthz`` passes through ``BearerAuth`` first; the MCP app
is mounted *inside* that middleware, so an unauthenticated MCP request never
reaches the MCP session manager.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import Principal, TokenRegistry, bearer_from_header
from .service import MemoryService, ServiceError

log = logging.getLogger("surooh_memory")

MAX_REST_BODY = 64 * 1024
_PUBLIC_PATHS = frozenset({"/healthz"})


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):  # defence in depth; middleware sets it
        raise PermissionError("unauthenticated")
    return principal


class BearerAuth:
    """Pure ASGI middleware: 401 unless a configured bearer token is presented."""

    def __init__(self, app: ASGIApp, registry: TokenRegistry) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or (scope["type"] == "http" and scope.get("path") in _PUBLIC_PATHS):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # No websocket (or other) surface exists; never pass one through unauthenticated.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        header = None
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                header = v.decode("latin-1")
                break
        principal = self.registry.authenticate(bearer_from_header(header))
        if principal is None:
            resp = JSONResponse(
                {"error": "unauthorized", "detail": "valid bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="surooh-memory"'},
            )
            await resp(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


def _error(exc: ServiceError, *, write: bool) -> JSONResponse:
    body: dict[str, Any] = {"error": exc.code, "detail": exc.detail}
    if write:
        body["saved"] = False
    return JSONResponse(body, status_code=exc.status)


async def _json_body(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise _BodyError(400, "invalid Content-Length")
        if int(declared) > MAX_REST_BODY:
            raise _BodyError(413, "body too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REST_BODY:
            raise _BodyError(413, "body too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        data = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _BodyError(400, "body must be JSON") from None
    if not isinstance(data, dict):
        raise _BodyError(400, "body must be a JSON object")
    return data


class _BodyError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status, self.detail = status, detail


_REMEMBER_FIELDS = ("company_id", "project_id", "text", "speaker", "source_ref", "kind", "subject_key")


def _build_mcp(service: MemoryService) -> MCPServer:
    mcp = MCPServer(
        "surooh-memory",
        instructions=(
            "Shared Surooh memory. Use `recall` before answering questions about prior decisions; "
            "use `remember` only for facts the user stated or approved (speaker='user') - anything "
            "you propose yourself must use speaker='assistant_suggestion'. Never store secrets. "
            "A memory is saved only if the result says saved: true."
        ),
    )

    def principal_of(ctx: Context) -> Principal:
        req = ctx.request_context.request
        p = getattr(getattr(req, "state", None), "principal", None)
        if not isinstance(p, Principal):
            raise ToolError(json.dumps({"error": "unauthorized"}))
        return p

    def fail(exc: ServiceError, *, write: bool) -> ToolError:
        body: dict[str, Any] = {"error": exc.code, "detail": exc.detail}
        if write:
            body["saved"] = False
        return ToolError(json.dumps(body))

    @mcp.tool(description=(
        "Save a fact or decision to shared Surooh memory. speaker is required: 'user' only for what the "
        "user stated or approved, 'assistant_suggestion' for your own proposals. Returns saved: true only on success."
    ))
    async def remember(
        ctx: Context,
        company_id: str,
        project_id: str,
        text: str,
        source_ref: str,
        speaker: Literal["user", "assistant_suggestion"],
        kind: str = "fact",
        subject_key: str | None = None,
    ) -> dict[str, Any]:
        p = principal_of(ctx)
        try:
            result = await run_in_threadpool(
                service.remember, p, company_id=company_id, project_id=project_id, text=text,
                speaker=speaker, source_ref=source_ref, kind=kind, subject_key=subject_key,
            )
        except ServiceError as exc:
            raise fail(exc, write=True) from None
        return result.public()

    @mcp.tool(description="Recall facts from shared Surooh memory within your scope. Unapproved assistant suggestions are labelled.")
    async def recall(
        ctx: Context,
        company_id: str,
        project_id: str | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        p = principal_of(ctx)
        try:
            return await run_in_threadpool(
                service.recall, p, company_id=company_id, project_id=project_id, query=query, limit=limit
            )
        except ServiceError as exc:
            raise fail(exc, write=False) from None

    @mcp.tool(description="Permanently forget a fact by id. Its text is erased and cannot be saved again.")
    async def forget(ctx: Context, id: str) -> dict[str, Any]:
        p = principal_of(ctx)
        try:
            return await run_in_threadpool(service.forget, p, id)
        except ServiceError as exc:
            raise fail(exc, write=True) from None

    return mcp


def create_app(
    service: MemoryService,
    registry: TokenRegistry,
    *,
    allowed_hosts: list[str] | None = None,
) -> Starlette:
    """Build the ASGI app. ``registry`` is mandatory: there is no unauthenticated mode."""
    if not isinstance(registry, TokenRegistry):
        raise TypeError("a TokenRegistry is required")

    mcp = _build_mcp(service)
    hosts = allowed_hosts or ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        max_request_body_size=MAX_REST_BODY,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts],
        ),
    )

    async def healthz(request: Request) -> Response:
        try:
            await run_in_threadpool(service.store.ping)
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": True})

    async def post_fact(request: Request) -> Response:
        try:
            body = await _json_body(request)
        except _BodyError as e:
            return JSONResponse({"error": "bad_request", "detail": e.detail, "saved": False}, status_code=e.status)
        unknown = set(body) - set(_REMEMBER_FIELDS)
        if unknown:
            return JSONResponse(
                {"error": "invalid_input", "detail": f"unknown fields: {sorted(unknown)}", "saved": False},
                status_code=422,
            )
        kwargs = {k: body.get(k) for k in _REMEMBER_FIELDS}
        kwargs["kind"] = kwargs["kind"] or "fact"
        try:
            result = await run_in_threadpool(service.remember, _principal(request), **kwargs)
        except ServiceError as exc:
            return _error(exc, write=True)
        return JSONResponse(result.public(), status_code=201 if result.created else 200)

    async def get_facts(request: Request) -> Response:
        q = request.query_params
        try:
            data = await run_in_threadpool(
                service.recall, _principal(request), company_id=q.get("company_id"),
                project_id=q.get("project_id"), query=q.get("query"), limit=q.get("limit", 20),
            )
        except ServiceError as exc:
            return _error(exc, write=False)
        return JSONResponse(data)

    async def delete_fact(request: Request) -> Response:
        try:
            data = await run_in_threadpool(service.forget, _principal(request), request.path_params["fact_id"])
        except ServiceError as exc:
            return _error(exc, write=True)
        return JSONResponse(data)

    async def get_export(request: Request) -> Response:
        q = request.query_params
        try:
            data = await run_in_threadpool(
                service.export, _principal(request), company_id=q.get("company_id"), project_id=q.get("project_id")
            )
        except ServiceError as exc:
            return _error(exc, write=False)
        return JSONResponse(data)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    inner = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/v1/facts", post_fact, methods=["POST"]),
            Route("/v1/facts", get_facts, methods=["GET"]),
            Route("/v1/facts/{fact_id}", delete_fact, methods=["DELETE"]),
            Route("/v1/export", get_export, methods=["GET"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    inner.add_middleware(BearerAuth, registry=registry)
    inner.state.mcp = mcp
    return inner
