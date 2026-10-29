"""Fail-loud REST client for future capture hooks.

Contract: ``remember`` returns only when the server confirmed ``saved: true``.
Any transport failure, timeout, 5xx or malformed response raises
``MemoryUnavailable``; a client-side rejection (422/403/409/...) raises
``MemoryRejected``. Nothing here ever reports a save that did not happen.
"""

from __future__ import annotations

from typing import Any

import httpx2


class MemoryError(Exception):  # noqa: A001 - module-scoped name
    pass


class MemoryUnavailable(MemoryError):
    """The service could not be reached or did not confirm the operation."""


class MemoryRejected(MemoryError):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class MemoryClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("token is required")
        self._http = httpx2.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MemoryClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx2.HTTPError as exc:
            raise MemoryUnavailable(f"memory service unreachable: {type(exc).__name__}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise MemoryUnavailable(f"memory service returned non-JSON (HTTP {resp.status_code})") from None
        if not isinstance(data, dict):
            raise MemoryUnavailable("memory service returned an unexpected body")
        if resp.status_code >= 500:
            raise MemoryUnavailable(f"memory service error {resp.status_code}: {data.get('error', 'unknown')}")
        if resp.status_code >= 400:
            raise MemoryRejected(resp.status_code, str(data.get("error", "error")), str(data.get("detail", "")))
        return data

    def remember(
        self,
        *,
        company_id: str,
        project_id: str,
        text: str,
        source_ref: str,
        speaker: str,
        kind: str = "fact",
        subject_key: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "company_id": company_id, "project_id": project_id, "text": text, "source_ref": source_ref,
            "speaker": speaker, "kind": kind, "subject_key": subject_key,
        }
        data = self._call("POST", "/v1/facts", json=body)
        if data.get("saved") is not True or not data.get("id"):
            raise MemoryUnavailable("memory service did not confirm the save")
        return data

    def recall(
        self, *, company_id: str, project_id: str | None = None, query: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"company_id": company_id, "limit": limit}
        if project_id:
            params["project_id"] = project_id
        if query:
            params["query"] = query
        data = self._call("GET", "/v1/facts", params=params)
        facts = data.get("facts")
        if not isinstance(facts, list):
            raise MemoryUnavailable("memory service returned no fact list")
        return facts

    def forget(self, fact_id: str) -> None:
        data = self._call("DELETE", f"/v1/facts/{fact_id}")
        if data.get("forgotten") is not True:
            raise MemoryUnavailable("memory service did not confirm the forget")
