"""Visibility inside every query: deny, allow_only and [] on both source kinds,
owner-hidden transactions and denied companies, across lists, gets, search
and every aggregate; hidden == missing; resource_ref on every row.

The strongest check is an equivalence: every read action, run with a card
hidden, must answer exactly what it answers on a twin database where that
card was never uploaded. A hidden card then cannot move a total, appear in
a merchant's card list, or show in any count.
"""

import pytest
from aab_plugin_runtime import serve
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_finance.adapter import FinanceAdapter
from aab_plugin_finance.clock import Clock
from aab_plugin_finance.store import Store

from .conftest import PLUGIN_TOKEN, data, items, scope
from .fakes import BANK, BANK_TXNS, CAL, CAL_TXNS, MAX, MAX_TXNS, ingest, seed, txn

NOT_FOUND = {"error": "not found"}

# The hidden card holds a bit of everything: a subscription, an installment
# plan, foreign spend, a transfer, a note, a pending row.
HIDDEN_TXNS = MAX_TXNS + [
    txn(f"2026-0{m}-11", -30.00, "SECRET STREAMING", category="Streaming") for m in (5, 6, 7, 8)
] + [
    txn("2026-08-03", -100.00, "SECRET TV", charged=-100.00, installments=(1, 4)),
    txn("2026-09-03", -100.00, "SECRET TV", charged=-100.00, installments=(2, 4)),
    txn("2026-09-04", -80.00, "SECRET HOTEL", currency="GBP", charged=-380.00),
    txn("2026-09-05", -70.00, "העברה סודית"),
    txn("2026-10-05", -5.00, "SECRET PENDING", status="pending"),
]
VISIBLE_TXNS = CAL_TXNS + [
    txn(f"2026-0{m}-12", -20.00, "PUBLIC STREAMING", category="Streaming") for m in (5, 6, 7, 8)
]
READS = [
    ("snapshot_info", {}), ("list_sources", {}), ("list_transactions", {"limit": 500}),
    ("search_transactions", {"query": "STREAMING"}), ("search_transactions", {"query": "et"}),
    ("monthly_summary", {"year": 2026}), ("monthly_summary", {"group_by": "category"}),
    ("monthly_summary", {"group_by": "none"}), ("by_category", {}), ("top_merchants", {}),
    ("recurring_merchants", {}), ("subscriptions", {}), ("installments", {"active_only": False}),
    ("foreign_currency", {}), ("largest_purchases", {}), ("list_notes", {}), ("list_runs", {}),
    ("list_refresh_requests", {"status": "all"}),
]


def _client(tmp_path, fake_now, name: str) -> TestClient:
    adapter = FinanceAdapter(Store(str(tmp_path / f"{name}.db")), Clock("Asia/Jerusalem", fake_now))
    app = serve([adapter], PLUGIN_TOKEN, tmp_path / f"{name}-secrets",
                Fernet.generate_key().decode(), service="finance")
    return TestClient(app, headers={"X-Plugin-Token": PLUGIN_TOKEN},
                      raise_server_exceptions=False)


def _performer(client):
    def perform(action, params=None, call_scope=None):
        return client.post("/perform", json={"action": action, "params": params or {},
                                             "scope": call_scope or scope()})
    return perform


def _note_first(perform, description, call_scope=None):
    tx = next(t for t in items(perform("list_transactions", {"limit": 500}))
              if t["description"] == description)
    assert perform("set_note", {"id": tx["id"], "note": f"note on {description}"},
                   call_scope).status_code == 200
    return tx["id"]


@pytest.fixture()
def worlds(tmp_path, fake_now):
    """(both, twin): `both` has the visible and the hidden card, `twin`
    only the visible one; same run ids, same clock, so answers compare."""
    both, twin = _performer(_client(tmp_path, fake_now, "both")), \
        _performer(_client(tmp_path, fake_now, "twin"))
    for world in (both, twin):
        ingest(world, "cal", "1234", VISIBLE_TXNS, run_id="run-visible-0001")
        _note_first(world, "NETFLIX")
    ingest(both, "max", "5678", HIDDEN_TXNS, run_id="run-hidden-0001")
    _note_first(both, "SECRET CLINIC")
    return both, twin


@pytest.mark.parametrize("action,params", READS)
@pytest.mark.parametrize("how", ["deny", "allow_only"])
def test_a_hidden_card_changes_nothing_anywhere(worlds, action, params, how):
    both, twin = worlds
    hidden = scope(card_deny=[MAX]) if how == "deny" else scope(card_allow=[CAL])
    assert data(both(action, params, hidden)) == data(twin(action, params))


def test_the_twin_check_is_not_vacuous(worlds):
    both, twin = worlds
    assert data(both("monthly_summary", {"year": 2026})) != data(twin("monthly_summary",
                                                                      {"year": 2026}))


def test_an_empty_allow_list_sees_no_cards(perform):
    seed(perform)
    none = scope(card_allow=[])
    assert {t["source"]["id"] for t in items(perform("list_transactions", {}, none))} == {BANK}
    assert [s["id"] for s in items(perform("list_sources", {}, none))] == [BANK]
    both_empty = scope(card_allow=[], account_allow=[])
    assert items(perform("list_transactions", {}, both_empty)) == []
    assert data(perform("monthly_summary", {}, both_empty))["totals"]["count"] == 0
    assert data(perform("snapshot_info", {}, both_empty))["transactions"] == 0


def test_accounts_answer_to_their_own_kind(perform):
    seed(perform)
    no_bank = scope(account_deny=[BANK])
    sources = {t["source"]["id"] for t in items(perform("list_transactions", {}, no_bank))}
    assert sources == {CAL, MAX}
    # A card allow list does not narrow accounts (each kind is its own dimension).
    only_cal = scope(card_allow=[CAL])
    assert {t["source"]["id"] for t in items(perform("list_transactions", {}, only_cal))} == {
        CAL, BANK}


def test_an_owner_hidden_transaction_is_gone_everywhere(perform):
    seed(perform)
    clinic = next(t for t in items(perform("list_transactions"))
                  if t["description"] == "SECRET CLINIC")
    hide = scope(tx_deny=[clinic["id"]])
    assert clinic["id"] not in {t["id"] for t in items(perform("list_transactions", {}, hide))}
    assert perform("get_transaction", {"id": clinic["id"]}, hide).json() == NOT_FOUND
    full = data(perform("monthly_summary", {}))["totals"]["net"]
    assert data(perform("monthly_summary", {}, hide))["totals"]["net"] == round(full - 500.0, 2)
    counted = {s["id"]: s["count"] for s in items(perform("list_sources", {}, hide))}
    assert counted[MAX] == len(MAX_TXNS) - 1


def test_a_denied_company_is_gone(perform):
    seed(perform)
    no_max = scope(company_deny=["max"])
    assert MAX not in {t["source"]["id"] for t in items(perform("list_transactions", {}, no_max))}
    assert MAX not in [s["id"] for s in items(perform("list_sources", {}, no_max))]


# ---- hidden == missing ---------------------------------------------------------------------

def test_a_hidden_transaction_is_the_same_404_as_a_missing_one(perform):
    seed(perform)
    clinic = next(t for t in items(perform("list_transactions"))
                  if t["description"] == "SECRET CLINIC")
    hidden = perform("get_transaction", {"id": clinic["id"]}, scope(card_deny=[MAX]))
    outside = perform("get_transaction", {"id": clinic["id"]}, scope(card_allow=[CAL]))
    missing = perform("get_transaction", {"id": "tx_0000000000000000"})
    assert hidden.status_code == outside.status_code == missing.status_code == 404
    assert hidden.json() == outside.json() == missing.json() == NOT_FOUND


@pytest.mark.parametrize("action", ["list_transactions", "search_transactions", "by_category",
                                    "top_merchants", "installments", "foreign_currency",
                                    "largest_purchases", "list_notes", "list_runs"])
def test_a_hidden_source_filter_is_the_same_404_as_a_missing_one(perform, action):
    seed(perform)
    params = {"query": "xx"} if action == "search_transactions" else {}
    hidden = perform(action, {**params, "source": MAX}, scope(card_deny=[MAX]))
    alias = perform(action, {**params, "source": "MAX 5678"}, scope(card_deny=[MAX]))
    missing = perform(action, {**params, "source": "max:9999"})
    assert hidden.status_code == alias.status_code == missing.status_code == 404
    assert hidden.json() == missing.json() == NOT_FOUND
    assert perform(action, {**params, "source": MAX}).status_code == 200


def test_a_malformed_source_filter_is_400(perform):
    assert perform("list_transactions", {"source": "not a card"}).status_code == 400


# ---- resource_ref ------------------------------------------------------------------------

def test_every_row_naming_a_source_carries_resource_ref(perform):
    seed(perform)
    ingest(perform, "cal", "1234", [txn("2026-09-02", -90, "TV", installments=(1, 3))])
    tx_id = items(perform("list_transactions"))[0]["id"]
    perform("set_note", {"id": tx_id, "note": "x"})

    def refs(rows):
        assert rows, "nothing to check"
        for row in rows:
            assert row["resource_ref"]["kind"] in ("card", "account"), row
            assert row["resource_ref"]["id"] in (CAL, MAX, BANK), row

    refs(items(perform("list_transactions")))
    refs(items(perform("search_transactions", {"query": "et"})))
    refs(items(perform("largest_purchases")))
    refs(items(perform("list_sources")))
    refs(items(perform("list_runs")))
    refs(items(perform("list_notes")))
    refs(data(perform("snapshot_info"))["sources"])
    refs(items(perform("installments")))
    summary = data(perform("monthly_summary"))
    refs(summary["groups"])
    refs([g for m in summary["months"] for g in m["groups"]])
    assert data(perform("get_transaction", {"id": tx_id}))["resource_ref"]["id"] in (CAL, MAX,
                                                                                    BANK)


def test_the_bank_dataset_is_an_account(perform):
    ingest(perform, "leumi", "123456", BANK_TXNS, kind="account")
    assert {t["resource_ref"]["kind"] for t in items(perform("list_transactions"))} == {
        "account"}
