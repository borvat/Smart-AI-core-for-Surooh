"""Business rules: write pipeline, recall, forget, export.

Write pipeline (``remember``), in order:
  1. validate fields and provenance
  2. scope check (principal must hold ``write`` on company/project)
  3. secret rejection (text, subject_key, source_ref) - nothing is stored
  4. normalization + content hash
  5. tombstone check - forgotten content cannot be re-captured
  6. dedup - same normalized text in scope returns the existing fact
  7. supersession - a newer *user* decision on the same subject_key replaces the
     active one; an *assistant suggestion* is stored as ``proposed`` and never
     supersedes anything
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import normalize as norm
from .auth import Principal
from .secrets_filter import contains_secret
from .store import Fact, Store

SPEAKERS = ("user", "assistant_suggestion")
KINDS = ("decision", "fact", "preference", "instruction", "note")
MAX_TEXT = 4000
MAX_SOURCE_REF = 256
MAX_LIMIT = 100
DEFAULT_LIMIT = 20

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SUBJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_FACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
UNAPPROVED_LABEL = "UNAPPROVED assistant suggestion - not a user decision"


class ServiceError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.code)
        self.detail = detail or self.code


class ValidationFailed(ServiceError):
    status, code = 422, "invalid_input"


class SecretDetected(ServiceError):
    status, code = 422, "secret_detected"


class Forbidden(ServiceError):
    status, code = 403, "forbidden"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class Forgotten(ServiceError):
    status, code = 409, "forgotten"


class StoreUnavailable(ServiceError):
    status, code = 503, "store_unavailable"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _require_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationFailed(f"{name} must match {_ID_RE.pattern}")
    return value


@dataclass(frozen=True)
class RememberResult:
    fact: Fact
    created: bool
    deduplicated: bool
    superseded_id: str | None

    def public(self) -> dict[str, Any]:
        return {
            "saved": True,
            "id": self.fact.id,
            "status": self.fact.status,
            "created": self.created,
            "deduplicated": self.deduplicated,
            "superseded_id": self.superseded_id,
            "fact": self.fact.public(),
        }


def _fts_query(text: str) -> str | None:
    toks = norm.tokens(text)
    if not toks:
        return None
    # Each token quoted (no FTS syntax injection), prefix-matched, OR-ed; bm25 ranks.
    return " OR ".join('"' + t.replace('"', '""') + '"*' for t in dict.fromkeys(toks))


class MemoryService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # -- write -------------------------------------------------------------

    def remember(
        self,
        principal: Principal,
        *,
        company_id: Any,
        project_id: Any,
        text: Any,
        speaker: Any,
        source_ref: Any,
        kind: Any = "fact",
        subject_key: Any = None,
    ) -> RememberResult:
        company_id = _require_id("company_id", company_id)
        project_id = _require_id("project_id", project_id)
        if speaker not in SPEAKERS:
            raise ValidationFailed(f"speaker must be one of {list(SPEAKERS)}")
        if kind not in KINDS:
            raise ValidationFailed(f"kind must be one of {list(KINDS)}")
        if not isinstance(text, str) or not text.strip():
            raise ValidationFailed("text is required")
        if len(text) > MAX_TEXT:
            raise ValidationFailed(f"text longer than {MAX_TEXT} characters")
        if not isinstance(source_ref, str) or not source_ref.strip() or len(source_ref) > MAX_SOURCE_REF:
            raise ValidationFailed(f"source_ref is required (max {MAX_SOURCE_REF} characters)")
        if subject_key is not None and (not isinstance(subject_key, str) or not _SUBJECT_RE.match(subject_key)):
            raise ValidationFailed(f"subject_key must match {_SUBJECT_RE.pattern}")

        if not principal.allows("write", company_id, project_id):
            raise Forbidden("token has no write access to this scope")

        hit = contains_secret(text, subject_key) or contains_secret(source_ref, entropy=False)
        if hit:
            raise SecretDetected(f"refused to store: looks like a credential ({hit})")

        norm_text = norm.normalize(text)
        if not norm_text:
            raise ValidationFailed("text has no searchable content")
        chash = norm.content_hash(norm_text)
        text = text.strip()
        source_ref = source_ref.strip()

        try:
            with self.store.transaction() as s:
                if s.is_tombstoned(company_id, project_id, chash):
                    raise Forgotten("this content was forgotten and cannot be saved again")

                existing = s.find_live_by_hash(company_id, project_id, chash)
                if existing is not None:
                    if not (speaker == "user" and existing.status == "proposed"):
                        return RememberResult(existing, created=False, deduplicated=True, superseded_id=None)
                    # The user stated an assistant's suggestion as their own decision:
                    # promote that row (no new row) and let it supersede the old decision.
                    now = _now()
                    subject = subject_key or existing.subject_key
                    old = s.active_for_subject(company_id, project_id, subject) if subject else None
                    if old is not None:
                        s.set_status(old.id, "superseded", now, superseded_by=existing.id)
                    s.set_status(
                        existing.id, "active", now, speaker="user",
                        supersedes=old.id if old else None, subject_key=subject,
                        source_assistant=principal.name, source_ref=source_ref,
                    )
                    promoted = s.get(existing.id)
                    assert promoted is not None
                    return RememberResult(promoted, created=False, deduplicated=True,
                                          superseded_id=old.id if old else None)

                now = _now()
                new_id = uuid.uuid4().hex
                old = None
                if speaker == "user" and subject_key is not None:
                    old = s.active_for_subject(company_id, project_id, subject_key)
                    if old is not None:
                        # Clear the unique "one active per subject" slot first.
                        s.set_status(old.id, "superseded", now, superseded_by=new_id)
                fact = s.insert(
                    id=new_id,
                    company_id=company_id,
                    project_id=project_id,
                    subject_key=subject_key,
                    kind=kind,
                    text=text,
                    content_hash=chash,
                    speaker=speaker,
                    status="active" if speaker == "user" else "proposed",
                    supersedes=old.id if old else None,
                    superseded_by=None,
                    source_assistant=principal.name,
                    source_ref=source_ref,
                    principal=principal.name,
                    created_at=now,
                    updated_at=now,
                    norm_text=norm_text,
                )
                return RememberResult(fact, created=True, deduplicated=False,
                                      superseded_id=old.id if old else None)
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"memory store error: {type(exc).__name__}") from exc

    # -- read --------------------------------------------------------------

    def _read_projects(self, principal: Principal, company_id: str, project_id: str | None) -> list[str] | None:
        if project_id is not None:
            _require_id("project_id", project_id)
            if not principal.allows("read", company_id, project_id):
                raise Forbidden("token has no read access to this scope")
            return [project_id]
        projects = principal.readable_projects(company_id)
        if projects == []:
            raise Forbidden("token has no read access to this company")
        return projects

    def recall(
        self,
        principal: Principal,
        *,
        company_id: Any,
        project_id: Any = None,
        query: Any = None,
        limit: Any = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        company_id = _require_id("company_id", company_id)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValidationFailed("limit must be an integer") from None
        if not 1 <= limit <= MAX_LIMIT:
            raise ValidationFailed(f"limit must be between 1 and {MAX_LIMIT}")
        if query is not None and (not isinstance(query, str) or len(query) > MAX_TEXT):
            raise ValidationFailed("query must be a string")
        projects = self._read_projects(principal, company_id, project_id)
        fts = _fts_query(query) if query else None
        if query and fts is None:
            return {"company_id": company_id, "project_id": project_id, "facts": []}

        try:
            with self.store.reading() as s:
                facts = s.live_facts(company_id, projects, fts, limit)
                items = []
                for f in facts:
                    item = f.public()
                    if f.status == "proposed":
                        item["label"] = UNAPPROVED_LABEL
                    prev = s.get(f.supersedes) if f.supersedes else None
                    item["updated_from"] = (
                        {"id": prev.id, "text": prev.text, "created_at": prev.created_at,
                         "source_assistant": prev.source_assistant}
                        if prev else None
                    )
                    items.append(item)
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"memory store error: {type(exc).__name__}") from exc
        # Approved user decisions first, unapproved suggestions after; rank order kept within each group.
        items.sort(key=lambda i: 0 if i["approved"] else 1)
        return {"company_id": company_id, "project_id": project_id, "facts": items}

    # -- forget ------------------------------------------------------------

    def forget(self, principal: Principal, fact_id: Any) -> dict[str, Any]:
        if not isinstance(fact_id, str) or not _FACT_ID_RE.match(fact_id):
            raise NotFound("no such fact")
        try:
            with self.store.transaction() as s:
                fact = s.get(fact_id)
                # Out-of-scope ids are reported exactly like unknown ids (no existence oracle).
                if fact is None or not principal.allows("write", fact.company_id, fact.project_id):
                    raise NotFound("no such fact")
                s.erase(fact, _now())
            self.store.checkpoint()
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"memory store error: {type(exc).__name__}") from exc
        return {"forgotten": True, "id": fact_id}

    # -- export ------------------------------------------------------------

    def export(self, principal: Principal, *, company_id: Any, project_id: Any = None) -> dict[str, Any]:
        company_id = _require_id("company_id", company_id)
        projects = self._read_projects(principal, company_id, project_id)
        try:
            with self.store.reading() as s:
                facts = [f.public() for f in s.all_facts(company_id, projects)]
                tombstones = s.tombstone_count(company_id, projects)
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"memory store error: {type(exc).__name__}") from exc
        return {
            "company_id": company_id,
            "project_id": project_id,
            "exported_at": _now(),
            "facts": facts,
            "forgotten_count": tombstones,
        }
