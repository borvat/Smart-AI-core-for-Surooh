"""Service-level tests for the I/O & edge-case matrix."""

from __future__ import annotations

import sqlite3

import pytest

from surooh_memory.service import (
    UNAPPROVED_LABEL,
    Forbidden,
    Forgotten,
    MemoryService,
    NotFound,
    SecretDetected,
    StoreUnavailable,
    ValidationFailed,
)
from surooh_memory.store import Store

from .conftest import COMPANY, make_principal

A = "project-a"
B = "project-b"


def remember(svc, p, text, *, project=A, speaker="user", subject=None, kind="decision", ref="chat:1"):
    return svc.remember(p, company_id=COMPANY, project_id=project, text=text, speaker=speaker,
                        source_ref=ref, kind=kind, subject_key=subject)


def recall(svc, p, **kw):
    kw.setdefault("company_id", COMPANY)
    return svc.recall(p, **kw)["facts"]


# -- provenance -----------------------------------------------------------

def test_every_fact_carries_provenance(service, claude):
    r = remember(service, claude, "Ship with PostNL", subject="shipping.carrier", ref="claude:conv-42")
    f = r.fact.public()
    for key in ("company_id", "project_id", "source_assistant", "source_ref", "created_at", "speaker"):
        assert f[key], key
    assert f["source_assistant"] == "claude"  # taken from the token, not the caller
    assert f["source_ref"] == "claude:conv-42"


# -- matrix: new decision ---------------------------------------------------

def test_new_decision_stored_active(service, claude):
    r = remember(service, claude, "Ship with PostNL", subject="shipping.carrier")
    assert r.created and r.fact.status == "active"
    assert r.public()["saved"] is True
    [got] = recall(service, claude, project_id=A)
    assert got["id"] == r.fact.id and got["approved"] is True


# -- matrix: update decision ------------------------------------------------

def test_newer_user_decision_supersedes(service, claude, store):
    old = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    new = remember(service, claude, "Ship with DHL from November", subject="shipping.carrier").fact
    assert new.supersedes == old.id
    old_row = store.get(old.id)
    assert old_row.status == "superseded" and old_row.superseded_by == new.id
    facts = recall(service, claude, project_id=A)
    assert [f["id"] for f in facts] == [new.id]  # only latest active per subject
    assert facts[0]["updated_from"]["id"] == old.id
    assert facts[0]["updated_from"]["text"] == "Ship with PostNL"
    assert facts[0]["updated_from"]["created_at"] == old.created_at


def test_supersession_is_per_scope(service, claude):
    a = remember(service, claude, "Ship with PostNL", subject="shipping.carrier", project=A).fact
    remember(service, claude, "Ship with DHL", subject="shipping.carrier", project=B)
    assert recall(service, claude, project_id=A)[0]["id"] == a.id


# -- matrix: assistant suggestion ------------------------------------------

def test_assistant_suggestion_is_proposed_and_labelled(service, claude):
    dec = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    sug = remember(service, claude, "Maybe switch to DHL to save cost", subject="shipping.carrier",
                   speaker="assistant_suggestion").fact
    assert sug.status == "proposed" and sug.supersedes is None
    facts = {f["id"]: f for f in recall(service, claude, project_id=A)}
    assert facts[dec.id]["status"] == "active"           # decision untouched
    assert facts[sug.id]["approved"] is False
    assert facts[sug.id]["label"] == UNAPPROVED_LABEL
    # approved decisions listed before unapproved suggestions
    assert list(facts)[0] == dec.id


def test_suggestion_never_supersedes_user_decision(service, claude, store):
    dec = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    for i in range(3):
        remember(service, claude, f"Consider carrier option {i}", subject="shipping.carrier",
                 speaker="assistant_suggestion")
    assert store.get(dec.id).status == "active"


def test_user_restating_suggestion_promotes_it(service, claude, store):
    dec = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    sug = remember(service, claude, "Ship with DHL", subject="shipping.carrier",
                   speaker="assistant_suggestion").fact
    r = remember(service, claude, "ship with DHL", subject="shipping.carrier")  # user approves it
    assert r.fact.id == sug.id and r.fact.status == "active" and r.fact.speaker == "user"
    assert r.superseded_id == dec.id
    assert store.get(dec.id).status == "superseded"


def test_suggestion_duplicate_of_decision_does_not_downgrade(service, claude, store):
    dec = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    r = remember(service, claude, "ship with postnl", subject="shipping.carrier", speaker="assistant_suggestion")
    assert r.deduplicated and r.fact.id == dec.id and store.get(dec.id).status == "active"


# -- matrix: duplicates -----------------------------------------------------

@pytest.mark.parametrize("dup", ["Ship with PostNL", "ship  with postnl!", "SHIP WITH POSTNL."])
def test_exact_and_near_duplicate(service, claude, store, dup):
    first = remember(service, claude, "Ship with PostNL").fact
    r = remember(service, claude, dup)
    assert r.deduplicated and not r.created and r.fact.id == first.id
    assert len(store.all_facts(COMPANY, None)) == 1


def test_arabic_near_duplicate(service, claude, store):
    first = remember(service, claude, "نشحن الطلبات عبر PostNL").fact
    r = remember(service, claude, "نشحن طلبات عبر postnl")
    assert r.fact.id == first.id and len(store.all_facts(COMPANY, None)) == 1


def test_same_text_other_project_is_not_duplicate(service, claude):
    a = remember(service, claude, "Ship with PostNL", project=A).fact
    b = remember(service, claude, "Ship with PostNL", project=B).fact
    assert a.id != b.id


# -- matrix: secrets ----------------------------------------------------------

def test_secret_rejected_nothing_stored(service, claude, store):
    with pytest.raises(SecretDetected) as ei:
        remember(service, claude, "the bol api_key=" + "Zx9" * 8)
    assert ei.value.status == 422 and ei.value.code == "secret_detected"
    assert "Zx9" not in ei.value.detail  # value never echoed
    assert store.all_facts(COMPANY, None) == []


def test_secret_in_source_ref_rejected(service, claude, store):
    with pytest.raises(SecretDetected):
        remember(service, claude, "harmless", ref="https://u:" + "pw" * 5 + "@host/x")
    assert store.all_facts(COMPANY, None) == []


# -- matrix: scope ------------------------------------------------------------

def test_out_of_scope_write_rejected(service):
    p = make_principal("chatgpt", [f"{COMPANY}/{A}"])
    with pytest.raises(Forbidden) as ei:
        remember(service, p, "x fact", project=B)
    assert ei.value.status == 403
    with pytest.raises(Forbidden):
        service.remember(p, company_id="other-co", project_id=A, text="x", speaker="user", source_ref="r")


def test_read_only_token_cannot_write(service):
    p = make_principal("reader", [f"{COMPANY}/{A}"], perms=("read",))
    with pytest.raises(Forbidden):
        remember(service, p, "x fact")


def test_out_of_scope_read_rejected_or_filtered(service, claude):
    remember(service, claude, "A fact about project A", project=A)
    remember(service, claude, "B fact about project B", project=B)
    p = make_principal("chatgpt", [f"{COMPANY}/{A}"])
    with pytest.raises(Forbidden):
        recall(service, p, project_id=B)
    # company-wide recall is filtered down to readable projects
    facts = recall(service, p)
    assert {f["project_id"] for f in facts} == {A}
    with pytest.raises(Forbidden):
        recall(service, p, company_id="other-co")


# -- acceptance: project isolation ------------------------------------------

def test_project_a_recall_never_shows_project_b(service, claude):
    remember(service, claude, "Carrier is PostNL", project=A, subject="shipping.carrier")
    remember(service, claude, "Carrier is DHL", project=B, subject="shipping.carrier")
    remember(service, claude, "Carrier suggestion UPS", project=B, speaker="assistant_suggestion")
    for query in (None, "carrier", "DHL", "UPS"):
        facts = recall(service, claude, project_id=A, query=query)
        assert all(f["project_id"] == A for f in facts), query
    assert recall(service, claude, project_id=A, query="DHL") == []


# -- matrix: forget -----------------------------------------------------------

def test_forget_erases_and_blocks_recapture(service, claude, store, tmp_path):
    keep = remember(service, claude, "Keep this fact").fact
    f = remember(service, claude, "Supplier contact is Ahmad at Nan Trading", subject="supplier.contact").fact
    assert service.forget(claude, f.id) == {"forgotten": True, "id": f.id}
    assert store.get(f.id) is None
    assert all(x["id"] != f.id for x in recall(service, claude, project_id=A))
    assert recall(service, claude, project_id=A, query="Ahmad") == []
    with pytest.raises(Forgotten):
        remember(service, claude, "supplier contact is ahmad at nan trading")
    with pytest.raises(Forgotten):
        remember(service, claude, "Supplier contact is Ahmad at Nan Trading", speaker="assistant_suggestion")
    exp = service.export(claude, company_id=COMPANY)
    assert [x["id"] for x in exp["facts"]] == [keep.id] and exp["forgotten_count"] == 1
    # text erased from the database file itself, not just hidden
    store.checkpoint()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("mem.db"))
    assert b"Ahmad" not in raw and "ahmad".encode() not in raw
    assert b"Keep this fact" in raw


def test_forget_superseded_chain_link(service, claude, store):
    old = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    new = remember(service, claude, "Ship with DHL", subject="shipping.carrier").fact
    service.forget(claude, old.id)
    [got] = recall(service, claude, project_id=A)
    assert got["id"] == new.id and got["updated_from"] is None and store.get(new.id).supersedes is None


def test_forget_unknown_or_out_of_scope_is_404(service, claude):
    with pytest.raises(NotFound) as ei:
        service.forget(claude, "0" * 32)
    assert ei.value.status == 404
    with pytest.raises(NotFound):
        service.forget(claude, "not-an-id")
    f = remember(service, claude, "B only fact", project=B).fact
    with pytest.raises(NotFound):
        service.forget(make_principal("chatgpt", [f"{COMPANY}/{A}"]), f.id)


# -- matrix: store down ---------------------------------------------------------

def test_store_error_reports_not_saved(service, claude, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(service.store, "insert", boom)
    with pytest.raises(StoreUnavailable) as ei:
        remember(service, claude, "a fact that cannot be saved")
    assert ei.value.status == 503


def test_closed_store_reports_unavailable(tmp_path, claude):
    s = Store(str(tmp_path / "x.db"))
    svc = MemoryService(s)
    s.close()
    with pytest.raises(StoreUnavailable):
        remember(svc, claude, "anything")
    with pytest.raises(StoreUnavailable):
        recall(svc, claude, project_id=A)


# -- acceptance: restart ----------------------------------------------------------

def test_restart_same_db_recalls(tmp_path, claude):
    path = str(tmp_path / "persist.db")
    s1 = Store(path)
    f = remember(MemoryService(s1), claude, "Ship with PostNL", subject="shipping.carrier").fact
    s1.close()
    s2 = Store(path)
    try:
        [got] = recall(MemoryService(s2), claude, project_id=A)
        assert got["id"] == f.id and got["text"] == "Ship with PostNL"
    finally:
        s2.close()


# -- validation ------------------------------------------------------------------

@pytest.mark.parametrize(
    "kw",
    [
        {"text": ""},
        {"text": "x" * 5000},
        {"speaker": "system"},
        {"ref": ""},
        {"subject": "Bad Key!"},
        {"project": "../etc"},
        {"kind": "secret"},
    ],
)
def test_validation(service, claude, kw):
    args = {"text": "valid text", **kw}
    text = args.pop("text")
    with pytest.raises(ValidationFailed):
        remember(service, claude, text, **args)


def test_search_query_cannot_inject_fts_syntax(service, claude):
    remember(service, claude, "Ship with PostNL")
    for q in ('"', "NEAR(", "a OR", "*", "postnl) OR (x", "-"):
        recall(service, claude, project_id=A, query=q)  # must not raise
    assert recall(service, claude, project_id=A, query="postnl")[0]["text"] == "Ship with PostNL"


def test_arabic_search(service, claude):
    remember(service, claude, "نشحن جميع الطلبات عبر PostNL")
    assert recall(service, claude, project_id=A, query="طلبات")[0]["text"].startswith("نشحن")
    assert recall(service, claude, project_id=A, query="الطلبات")[0]["text"].startswith("نشحن")


def test_user_restating_suggestion_with_other_subject_is_active(service, claude, store):
    sug = remember(service, claude, "Use Sendcloud for labels", speaker="assistant_suggestion").fact
    r = remember(service, claude, "use sendcloud for labels", subject="shipping.labels")
    assert r.fact.id == sug.id and r.fact.status == "active" and r.fact.subject_key == "shipping.labels"


def test_reverting_to_earlier_decision_creates_new_active(service, claude, store):
    a = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    b = remember(service, claude, "Ship with DHL", subject="shipping.carrier").fact
    c = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    assert c.id not in (a.id, b.id) and c.supersedes == b.id
    [got] = recall(service, claude, project_id=A)
    assert got["id"] == c.id and got["updated_from"]["text"] == "Ship with DHL"


def test_ean_codes_are_not_secrets(service, claude):
    for ean in ("8712345678906", "5901234123457", "08712345678906"):
        assert remember(service, claude, f"EAN {ean} belongs to the blue kettle").created


def test_symbols_change_meaning_and_hash(service, claude, store):
    a = remember(service, claude, "Margin must be > 10%").fact
    b = remember(service, claude, "Margin must be < 10%").fact
    c = remember(service, claude, "Price 10 €").fact
    d = remember(service, claude, "Price 10 $").fact
    e = remember(service, claude, "Temperature -5").fact
    f = remember(service, claude, "Temperature 5").fact
    assert len({a.id, b.id, c.id, d.id, e.id, f.id}) == 6
    assert recall(service, claude, project_id=A, query="margin > 10%")  # symbol-only tokens don't break FTS


def test_user_attaching_subject_to_existing_fact_supersedes(service, claude, store):
    dec = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    plain = remember(service, claude, "Ship with DHL").fact  # no subject yet
    r = remember(service, claude, "ship with DHL", subject="shipping.carrier")
    assert r.fact.id == plain.id and r.fact.subject_key == "shipping.carrier"
    assert r.superseded_id == dec.id and store.get(dec.id).status == "superseded"
    [got] = recall(service, claude, project_id=A)
    assert got["id"] == plain.id and got["updated_from"]["id"] == dec.id


def test_forget_erases_superseded_copies_of_same_text(service, claude):
    first = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    remember(service, claude, "Ship with DHL", subject="shipping.carrier")
    again = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    assert again.id != first.id
    service.forget(claude, again.id)
    exp = service.export(claude, company_id=COMPANY)
    assert all("PostNL" not in x["text"] for x in exp["facts"])
    assert [x["text"] for x in exp["facts"]] == ["Ship with DHL"]


def test_limit_keeps_active_decisions_over_newer_suggestions(service, claude):
    dec = remember(service, claude, "Ship with PostNL", subject="shipping.carrier").fact
    for i in range(25):
        remember(service, claude, f"Ship option number {i} suggested", speaker="assistant_suggestion")
    facts = recall(service, claude, project_id=A, limit=20)
    assert facts[0]["id"] == dec.id
    facts = recall(service, claude, project_id=A, query="ship", limit=20)
    assert facts[0]["id"] == dec.id


def test_forget_succeeds_when_checkpoint_fails(service, claude, store, monkeypatch):
    f = remember(service, claude, "temporary fact").fact
    def boom():
        raise sqlite3.OperationalError("checkpoint failed")
    monkeypatch.setattr(store, "checkpoint", boom)
    assert service.forget(claude, f.id) == {"forgotten": True, "id": f.id}
    assert store.get(f.id) is None
