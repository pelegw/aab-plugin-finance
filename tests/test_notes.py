"""set_note and list_notes: write, clear, history, visibility, survival across uploads."""

import sqlite3

from .conftest import data, items, scope
from .fakes import CAL_TXNS, MAX, ingest, seed, txn


def tx(perform, description):
    return next(t for t in items(perform("list_transactions"))
                if t["description"] == description)


def test_a_note_is_written_shown_and_listed(perform):
    seed(perform)
    netflix = tx(perform, "NETFLIX")
    out = data(perform("set_note", {"id": netflix["id"], "note": "family plan"}))
    assert out == {"id": netflix["id"], "note": "family plan", "updated_at": out["updated_at"]}
    assert "resource_ref" not in out                 # a write result is never post-filtered
    assert data(perform("get_transaction", {"id": netflix["id"]}))["note"] == "family plan"
    assert tx(perform, "NETFLIX")["note"] == "family plan"
    assert tx(perform, "SUPER-PHARM")["note"] is None
    [listed] = items(perform("list_notes"))
    assert listed["note"] == "family plan" and listed["transaction"]["id"] == netflix["id"]
    assert listed["resource_ref"] == {"kind": "card", "id": "cal:1234"}


def test_an_empty_note_clears_it_and_history_keeps_both(perform, db_path):
    seed(perform)
    netflix = tx(perform, "NETFLIX")
    perform("set_note", {"id": netflix["id"], "note": "first"}, scope(request_id="req-1"))
    cleared = data(perform("set_note", {"id": netflix["id"], "note": ""},
                           scope(request_id="req-2")))
    assert cleared["note"] is None and items(perform("list_notes")) == []
    assert tx(perform, "NETFLIX")["note"] is None
    with sqlite3.connect(db_path) as conn:
        history = conn.execute("SELECT note, request_id FROM note_history ORDER BY id").fetchall()
    assert history == [("first", "req-1"), ("", "req-2")]


def test_a_malformed_request_id_is_not_stored(perform, db_path):
    seed(perform)
    perform("set_note", {"id": tx(perform, "NETFLIX")["id"], "note": "x"},
            scope(request_id="bad id\nforged"))
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT request_id FROM notes").fetchone() == (None,)


def test_a_note_on_a_hidden_or_missing_transaction_is_404(perform):
    seed(perform)
    clinic = tx(perform, "SECRET CLINIC")
    hidden = perform("set_note", {"id": clinic["id"], "note": "x"}, scope(card_deny=[MAX]))
    missing = perform("set_note", {"id": "tx_0000000000000000", "note": "x"})
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert items(perform("list_notes")) == []           # nothing was written


def test_notes_on_hidden_cards_are_not_listed(perform):
    seed(perform)
    perform("set_note", {"id": tx(perform, "SECRET CLINIC")["id"], "note": "private"})
    assert items(perform("list_notes", {}, scope(card_deny=[MAX]))) == []
    assert len(items(perform("list_notes"))) == 1


def test_a_note_survives_a_re_upload(perform):
    ingest(perform, "cal", "1234", CAL_TXNS)
    netflix = tx(perform, "NETFLIX")
    perform("set_note", {"id": netflix["id"], "note": "keep me"})
    ingest(perform, "cal", "1234", CAL_TXNS)
    assert tx(perform, "NETFLIX")["note"] == "keep me"


def test_a_note_goes_with_its_stale_pending_row(perform):
    ingest(perform, "cal", "1234", [txn("2026-09-10", -10, "PENDING", status="pending")])
    perform("set_note", {"id": tx(perform, "PENDING")["id"], "note": "about to go"})
    ingest(perform, "cal", "1234", [], rng=("2026-09-01", "2026-09-30"))
    assert items(perform("list_notes")) == []


def test_note_validation(perform):
    seed(perform)
    netflix = tx(perform, "NETFLIX")
    assert perform("set_note", {"id": netflix["id"], "note": "x" * 2001}).status_code == 400
    assert perform("set_note", {"id": "not-a-tx", "note": "x"}).status_code == 400
    assert perform("set_note", {"id": netflix["id"]}).status_code == 400
    assert perform("set_note", {"id": netflix["id"], "note": 5}).status_code == 400


def test_list_notes_filters_by_source(perform):
    seed(perform)
    perform("set_note", {"id": tx(perform, "NETFLIX")["id"], "note": "a"})
    perform("set_note", {"id": tx(perform, "SECRET CLINIC")["id"], "note": "b"})
    assert [n["note"] for n in items(perform("list_notes", {"source": MAX}))] == ["b"]
