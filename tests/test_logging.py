"""Nothing financial in the logs: a DEBUG sweep over every action.

The test plants values: a description, a memo, a category, amounts, a card
id, a row key, the tx ids made from them, a note and a refresh reason. These
values go through uploads, reads, searches, aggregates, notes, refresh
requests and the error paths. After that, no log record from any logger
contains any of them. The lines can carry only these items
(finance-plugin-plan.md 3.6): action, status, run id, chunk index, row
counts, refresh ids.
"""

import logging

from .conftest import data, items, scope
from .fakes import chunk_params, ingest, ingest_rows, make_rows, txn

DESCRIPTION = "PLANTED-DESC-7f3a9"
MEMO = "PLANTED-MEMO-41bc"
CATEGORY = "PLANTED-CAT-9d02"
NOTE = "PLANTED-NOTE-c41d"
REASON = "PLANTED-REASON-e7a1"
MESSAGE = "PLANTED-MSG-55f0"
CARD = "7351"                                   # source id plantco:7351
AMOUNT, CHARGED = -98765.43, -87654.32          # 9876543 and 8765432 hundredths


def planted_rows():
    return [txn("2026-09-01", AMOUNT, DESCRIPTION, charged=CHARGED, currency="EUR",
                category=CATEGORY, memo=MEMO, identifier="IDENT-3c8e"),
            txn("2026-09-02", -12.34, DESCRIPTION, installments=(1, 3), status="pending")]


def test_a_debug_sweep_finds_no_financial_content(perform, caplog):
    caplog.set_level(logging.DEBUG)
    answer = ingest(perform, "plantco", CARD, planted_rows(), run_id="run-planted-0001")
    assert answer["status"] == "applied"
    rows = items(perform("list_transactions"))
    tx_ids = [t["id"] for t in rows]
    keys = [r["key"] for r in make_rows("plantco", CARD, planted_rows())]
    perform("set_note", {"id": tx_ids[0], "note": NOTE})
    for action, params in [
            ("snapshot_info", {}), ("list_sources", {}), ("get_transaction", {"id": tx_ids[0]}),
            ("search_transactions", {"query": DESCRIPTION}), ("monthly_summary", {"year": 2026}),
            ("by_category", {}), ("top_merchants", {}), ("recurring_merchants", {}),
            ("subscriptions", {}), ("installments", {"active_only": False}),
            ("foreign_currency", {}), ("largest_purchases", {}), ("list_notes", {}),
            ("list_runs", {"source": f"plantco:{CARD}"}),
            ("list_transactions", {"source": f"plantco:{CARD}"})]:
        assert perform(action, params).status_code == 200, action
    # Refusals and errors log too; their lines must be just as clean.
    hidden = scope(card_deny=[f"plantco:{CARD}"])
    assert perform("get_transaction", {"id": tx_ids[0]}, hidden).status_code == 404
    assert perform("list_transactions", {"source": f"plantco:{CARD}"}, hidden).status_code == 404
    assert perform("set_note", {"id": tx_ids[1], "note": NOTE}, scope(notes=False)
                   ).status_code == 403
    bad = ingest_rows(make_rows("plantco", CARD, planted_rows()))
    bad[0]["date"] = "2027-01-01"
    assert perform("ingest_snapshot", chunk_params("plantco", f"plantco:{CARD}", bad, index=1,
                                                   total=1, run_id="run-planted-0002")
                   ).status_code == 400
    conflict = ingest_rows(make_rows("plantco", CARD, planted_rows()))
    assert perform("ingest_snapshot", chunk_params("plantco", "plantco:9999", conflict, index=1,
                                                   total=1, run_id="run-planted-0003")
                   ).status_code == 409
    rid = data(perform("request_refresh", {"start": "2026-09-01", "end": "2026-09-30",
                                           "reason": REASON}))["refresh_id"]
    perform("report_refresh", {"refresh_id": rid, "status": "running", "message": MESSAGE})
    perform("normalize", {})                         # 404 action name, logged by the runtime

    text = "\n".join(r.getMessage() + " " + str(r.args) for r in caplog.records)
    planted = [DESCRIPTION, MEMO, CATEGORY, NOTE, REASON, MESSAGE, "IDENT-3c8e",
               f"plantco:{CARD}", "plantco|", "9876543", "98765.43", "8765432", "87654.32",
               *tx_ids, *keys]
    leaked = [p for p in planted if p in text]
    assert leaked == [], leaked
    # ...while the lines that should be there are.
    assert "ingest run applied run_id=run-planted-0001 chunks=1 added=2" in text
    assert f"refresh request approved refresh_id={rid}" in text
    assert "perform plugin=finance action=list_transactions status=200" in text


def test_error_messages_never_echo_values(perform):
    rows = ingest_rows(make_rows("plantco", CARD, planted_rows()))
    rows[1]["date"] = "2031-12-31"
    r = perform("ingest_snapshot", chunk_params("plantco", f"plantco:{CARD}", rows, index=1,
                                                total=1, run_id="run-planted-0004"))
    assert r.status_code == 400 and "2031" not in r.text and DESCRIPTION not in r.text
    r = perform("list_transactions", {"source": "PLANTED source 7"})
    assert r.status_code == 400 and "PLANTED" not in r.text
    r = perform("get_transaction", {"id": "tx_PLANTED"})
    assert r.status_code == 400 and "PLANTED" not in r.text
