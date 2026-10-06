"""Constraints: date_window_days, detail (aggregate vs line_items), notes and
merchant_names, and the fail-closed handling of unknown or malformed ones."""

import re

import pytest
import yaml

from aab_plugin_finance.adapter import MANIFEST_PATH

from .conftest import data, items, scope
from .fakes import seed

MANIFEST = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
DETAIL = next(c for c in MANIFEST["constraints"] if c["name"] == "detail")
# finance-plugin-plan.md 3.3: the only actions `detail: aggregate` allows.
AGGREGATE_ALLOWED = {"snapshot_info", "list_sources", "monthly_summary", "by_category",
                     "foreign_currency", "list_runs", "list_refresh_requests"}
PARAMS = {"get_transaction": {"id": "tx_0000000000000000"}, "search_transactions": {"query": "ab"},
          "set_note": {"id": "tx_0000000000000000", "note": "x"}}


def tx(perform, description, call_scope=None):
    return next(t for t in items(perform("list_transactions", {}, call_scope))
                if t["description"] == description)


# ---- date_window_days ----------------------------------------------------------------------

def test_the_window_hides_older_transactions_everywhere(perform):
    seed(perform)
    window = scope(date_window_days=30)          # today 2026-10-06 -> from 2026-09-06
    dates = {t["date"] for t in items(perform("list_transactions", {}, window))}
    assert min(dates) >= "2026-09-06" and "2026-09-01" not in dates
    pharm = tx(perform, "SUPER-PHARM")
    assert perform("get_transaction", {"id": pharm["id"]}, window).json() == {"error": "not found"}
    assert perform("set_note", {"id": pharm["id"], "note": "x"}, window).status_code == 404
    assert items(perform("search_transactions", {"query": "PHARM"}, window)) == []
    totals = data(perform("monthly_summary", {}, window))["totals"]
    # Inside the window: NETFLIX 49.90, REFUND -30, CLINIC 500, transfer 1000.
    assert (totals["count"], totals["net"]) == (4, 1519.9)


def test_a_zero_day_window_is_today_only(perform):
    seed(perform)
    from .fakes import ingest, txn
    ingest(perform, "cal", "1234", [txn("2026-10-06", -1, "TODAY")])
    assert [t["description"] for t in items(perform("list_transactions", {},
                                                    scope(date_window_days=0)))] == ["TODAY"]


def test_no_window_means_all_history(perform):
    seed(perform)
    assert "2025-01-05" in {t["date"] for t in items(perform("list_transactions"))}


def test_a_window_wider_than_the_calendar_is_all_history(perform):
    seed(perform)
    huge = scope(date_window_days=10 ** 12)       # timedelta would overflow
    assert len(items(perform("list_transactions", {}, huge))) == 7


# ---- detail --------------------------------------------------------------------------------

def test_the_aggregate_set_matches_the_plan():
    reads = {a["name"] for a in MANIFEST["actions"] if a["side_effect"] == "read"}
    assert reads - set(DETAIL["applies_to"]) == AGGREGATE_ALLOWED


@pytest.mark.parametrize("action", sorted(set(DETAIL["applies_to"])))
def test_aggregate_detail_refuses_line_items(perform, action):
    seed(perform)
    r = perform(action, PARAMS.get(action, {}), scope(detail="aggregate"))
    assert r.status_code == 403 and r.json() == {"error": "line items are outside your grant"}


@pytest.mark.parametrize("action", sorted(AGGREGATE_ALLOWED))
def test_aggregate_detail_allows_totals(perform, action):
    seed(perform)
    assert perform(action, {}, scope(detail="aggregate")).status_code == 200


def test_line_items_detail_is_the_top(perform):
    seed(perform)
    assert perform("list_transactions", {}, scope(detail="line_items")).status_code == 200


# ---- notes ---------------------------------------------------------------------------------

def test_notes_false_hides_and_refuses_notes(perform):
    seed(perform)
    netflix = tx(perform, "NETFLIX")
    perform("set_note", {"id": netflix["id"], "note": "shared family plan"})
    off = scope(notes=False)
    assert all("note" not in t for t in items(perform("list_transactions", {}, off)))
    assert "note" not in data(perform("get_transaction", {"id": netflix["id"]}, off))
    assert items(perform("search_transactions", {"query": "family"}, off)) == []
    assert items(perform("search_transactions", {"query": "family"}))[0]["id"] == netflix["id"]
    assert perform("set_note", {"id": netflix["id"], "note": "y"}, off).status_code == 403
    assert perform("list_notes", {}, off).status_code == 403


# ---- merchant_names ----------------------------------------------------------------------------

TOKEN = re.compile(r"merchant:[0-9a-f]{10}")


def test_merchant_names_false_replaces_descriptions(perform):
    seed(perform)
    off = scope(merchant_names=False)
    rows = items(perform("list_transactions", {}, off))
    assert all(TOKEN.fullmatch(t["description"]) and t["memo"] == "" for t in rows)
    assert {t["category"] for t in rows} >= {"Pharmacy", "Streaming"}       # categories stay
    netflix = tx(perform, "NETFLIX")
    assert TOKEN.fullmatch(data(perform("get_transaction", {"id": netflix["id"]}, off))[
        "description"])
    assert all(TOKEN.fullmatch(m["merchant"])
               for m in items(perform("top_merchants", {}, off)))
    assert all(TOKEN.fullmatch(t["description"])
               for t in items(perform("largest_purchases", {}, off)))
    perform("set_note", {"id": netflix["id"], "note": "x"})
    [note] = items(perform("list_notes", {}, off))
    assert TOKEN.fullmatch(note["transaction"]["description"])


def test_the_opaque_id_is_stable_per_merchant(perform):
    from .fakes import ingest, txn
    ingest(perform, "cal", "1234", [txn("2026-09-01", -1, "Coffee  Shop"),
                                    txn("2026-09-02", -2, "COFFEE SHOP"),
                                    txn("2026-09-03", -3, "TEA HOUSE")])
    off = scope(merchant_names=False)
    names = [t["description"] for t in items(perform("list_transactions", {}, off))]
    assert names[1] == names[2] != names[0]          # newest first: TEA, then the two coffees


def test_search_cannot_match_a_redacted_description(perform):
    seed(perform)
    off = scope(merchant_names=False)
    assert items(perform("search_transactions", {"query": "NETFLIX"}, off)) == []
    assert items(perform("search_transactions", {"query": "monthly plan"}, off)) == []  # memo
    assert len(items(perform("search_transactions", {"query": "Streaming"}, off))) == 1


# ---- fail closed -------------------------------------------------------------------------

@pytest.mark.parametrize("constraints", [
    {"spend_cap": 5},                       # not declared by this manifest
    {"date_window_days": -1},
    {"date_window_days": True},
    {"detail": "everything"},
    {"notes": "false"},
    {"merchant_names": 0},
])
def test_unknown_or_malformed_constraints_are_400(perform, constraints):
    r = perform("list_transactions", {}, scope(**constraints))
    assert r.status_code == 400
