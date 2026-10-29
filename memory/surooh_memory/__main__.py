"""Entry point.

    python -m surooh_memory serve [--db PATH] [--host 127.0.0.1] [--port 8765]
    python -m surooh_memory hash-token          # reads a token from stdin, prints its sha256

Configuration (environment):
    SUROOH_MEMORY_TOKENS / SUROOH_MEMORY_TOKENS_FILE  token hashes + scopes (required)
    SUROOH_MEMORY_DB                                   SQLite path (default ./surooh_memory.db)
    SUROOH_MEMORY_ALLOWED_HOSTS                        comma list of Host values for MCP
                                                       (default localhost only)
"""

from __future__ import annotations

import argparse
import os
import sys

from .auth import ConfigError, TokenRegistry, hash_token


def _serve(args: argparse.Namespace) -> int:
    try:
        registry = TokenRegistry.from_env()
    except ConfigError as exc:
        print(f"surooh-memory: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    from .app import create_app
    from .service import MemoryService
    from .store import Store

    hosts_env = os.environ.get("SUROOH_MEMORY_ALLOWED_HOSTS")
    hosts = [h.strip() for h in hosts_env.split(",") if h.strip()] if hosts_env else None
    app = create_app(MemoryService(Store(args.db)), registry, allowed_hosts=hosts)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", proxy_headers=False)
    return 0


def _hash(_: argparse.Namespace) -> int:
    token = sys.stdin.readline().strip()
    if len(token) < 32:
        print("token must be at least 32 characters", file=sys.stderr)
        return 2
    print(hash_token(token))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="surooh-memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--db", default=os.environ.get("SUROOH_MEMORY_DB", "surooh_memory.db"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=_serve)
    sub.add_parser("hash-token").set_defaults(func=_hash)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
