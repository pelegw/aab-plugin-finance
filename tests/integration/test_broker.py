"""The finance plugin end to end inside the gateway's broker.

finance-plugin-plan.md section 6, "Broker": the scraper key (A) uploads
through REST and the analysis key (B) reads; each is refused the other's
actions; a hidden card is the same 404 as a missing one and moves no total;
a refresh request is always a draft, approved by the owner, then claimed and
completed by A and seen by B; MCP lists finance tools per key. Plus the
broker's own guarantees around the plugin: the post-filter holds if the
plugin leaks, a 503 releases the write reservation, and a plugin offering
another manifest version is refused at discovery.
"""

import json
import os
import sqlite3

import pytest

from ..gateway import import_broker

_broker, _why = import_broker()
if _broker is None:
    if os.environ.get("AAB_REQUIRE_GATEWAY") == "1":
        raise RuntimeError(_why)
    pytest.skip(_why, allow_module_level=True)

from broker import db, hidden  # noqa: E402

from aab_plugin_finance import ingest as ingest_mod  # noqa: E402
from aab_plugin_finance.scope import View  # noqa: E402

from ..fakes import CAL_TXNS, MAX_TXNS, chunk_params, ingest_rows, make_rows  # noqa: E402
from .conftest import FINANCE_TOKEN, discover, serve_finance  # noqa: E402

ACT = "/v1/targets/finance/actions"
NOT_FOUND = {"error": "not found", "code": "not_found"}
ACCEPT = {"Accept": "application/json, text/event-stream"}
READS = ["snapshot_info", "list_sources", "list_transactions", "get_transaction",
         "search_transactions", "monthly_summary", "by_category", "top_merchants",
         "recurring_merchants", "subscriptions", "installments", "foreign_currency",
         "largest_purchases", "list_notes", "list_runs", "list_refresh_requests"]


def call(client, agent, action, params=None):
    return client.post(f"{ACT}/{action}", json={"params": params or {}}, headers=agent.headers)


def body(r):
    assert r.status_code == 200, r.text
    return r.json()


def upload(client, agent, company, card, txns, run_id):
    rows = ingest_rows(make_rows(company, card, txns))
    return call(client, agent, "ingest_snapshot",
                chunk_params(company, f"{company}:{card}", rows, index=1, total=1, run_id=run_id))


def seed(client, key_a):
    assert body(upload(client, key_a, "cal", "1234", CAL_TXNS, "run-cal-00001"))["added"] == 3
    assert body(upload(client, key_a, "max", "5678", MAX_TXNS, "run-max-00001"))["added"] == 2


def ledger_states() -> list[str]:
    with db.connect() as conn:
        return [r["state"] for r in conn.execute("SELECT state FROM capacity_ledger ORDER BY id")]


def outcomes() -> list[str]:
    with db.connect() as conn:
        return [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM decisions WHERE kind = 'outcome' ORDER BY id")]


# ---- A uploads, B reads -----------------------------------------------------------------

def test_key_a_ingests_and_key_b_reads(client, key_a, key_b):
    seed(client, key_a)
    assert ledger_states() == ["committed", "committed"]          # writes are metered
    assert [s["id"] for s in body(call(client, key_b, "list_sources"))["items"]] == [
        "cal:1234", "max:5678"]
    assert len(body(call(client, key_b, "list_transactions"))["items"]) == 5
    totals = body(call(client, key_b, "monthly_summary", {"year": 2026}))["totals"]
    assert (totals["count"], totals["net"]) == (5, 843.35)     # 123.45+49.90-30+200+500
    # The decision record keeps a params hash only: no transaction content in broker.db.
    with db.connect() as conn:
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM decisions")])
    assert "SUPER-PHARM" not in dump and "NETFLIX" not in dump and "1234|" not in dump


def test_each_key_is_refused_the_others_actions(client, key_a, key_b):
    seed(client, key_a)
    r = upload(client, key_b, "cal", "1234", CAL_TXNS, "run-cal-00002")
    assert r.status_code == 403 and r.json()["code"] == "out_of_grant"
    r = call(client, key_a, "list_transactions")
    assert r.status_code == 403 and r.json()["code"] == "out_of_grant"
    r = call(client, key_a, "list_refresh_requests")
    assert r.status_code == 200                                     # its own read


# ---- hidden == 404 ---------------------------------------------------------------------------

def test_a_hidden_card_is_404_and_absent_from_lists_and_aggregates(client, key_a, key_b,
                                                                  admin_headers):
    seed(client, key_a)
    before = body(call(client, key_b, "monthly_summary", {"year": 2026}))["totals"]["net"]
    clinic = next(t for t in body(call(client, key_b, "list_transactions"))["items"]
                  if t["description"] == "SECRET CLINIC")
    r = client.post("/v1/admin/hidden", json={"target": "finance", "kind": "card",
                                              "resource_id": "Max 5678"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["resource_id"] == "max:5678"    # plugin-normalized

    hid = call(client, key_b, "list_transactions", {"source": "max:5678"})
    gone = call(client, key_b, "list_transactions", {"source": "max:9999"})
    assert hid.status_code == gone.status_code == 404 and hid.json() == gone.json() == NOT_FOUND
    hid = call(client, key_b, "get_transaction", {"id": clinic["id"]})
    gone = call(client, key_b, "get_transaction", {"id": "tx_0000000000000000"})
    assert hid.status_code == gone.status_code == 404 and hid.json() == gone.json() == NOT_FOUND
    assert [s["id"] for s in body(call(client, key_b, "list_sources"))["items"]] == ["cal:1234"]
    assert "max:5678" not in {t["source"]["id"]
                              for t in body(call(client, key_b, "list_transactions"))["items"]}
    after = body(call(client, key_b, "monthly_summary", {"year": 2026}))
    assert after["totals"]["net"] == round(before - 700.0, 2)       # AMAZON 200 + CLINIC 500
    assert [g["source"]["id"] for g in after["groups"]] == ["cal:1234"]
    assert body(call(client, key_b, "top_merchants"))["items"][0]["merchant"] != "SECRET CLINIC"
    # Uploads for the hidden card still land (ingest is bound to the company).
    assert body(upload(client, key_a, "max", "5678", MAX_TXNS, "run-max-00002"))[
        "unchanged"] == 2


def test_a_key_deny_is_honoured_like_a_hidden_card(client, key_a, make_agent):
    seed(client, key_a)
    from .conftest import ANALYST_CAPS
    b = make_agent(ANALYST_CAPS, role="read-act", denies={"finance": {"card": ["max:5678"]}})
    assert "max:5678" not in {t["source"]["id"]
                              for t in body(call(client, b, "list_transactions"))["items"]}


def test_a_card_selector_reaches_the_plugin_as_allow_only(client, key_a, make_agent):
    seed(client, key_a)
    b = make_agent([{"target": "finance", "actions": ["read_*"],
                     "selector": {"card": ["cal:1234"]}}], role="read-act")
    assert {t["source"]["id"] for t in body(call(client, b, "list_transactions"))["items"]} == {
        "cal:1234"}
    assert body(call(client, b, "monthly_summary", {"year": 2026}))["totals"]["net"] == 143.35


def test_constraints_reach_the_plugin(client, key_a, make_agent):
    seed(client, key_a)
    b = make_agent([{"target": "finance", "actions": ["read_*"],
                     "constraints": {"detail": "aggregate"}}], role="read-act")
    r = call(client, b, "list_transactions")
    assert r.status_code == 403 and "line items" in r.json()["error"]
    assert call(client, b, "monthly_summary", {"year": 2026}).status_code == 200


# ---- refresh requests: always a draft, the owner approves ----------------------------------

def test_refresh_request_approval_claim_and_completion(client, key_a, key_b, admin_headers):
    r = call(client, key_b, "request_refresh", {"company": "all", "start": "2026-10-01",
                                                "end": "2026-10-31", "reason": "October review"})
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    action_id = r.json()["action_id"]
    assert body(call(client, key_a, "list_refresh_requests"))["items"] == []    # not yet approved
    done = client.post(f"/v1/admin/actions/{action_id}/approve", headers=admin_headers)
    assert done.status_code == 200 and done.json()["status"] == "done"
    refresh_id = done.json()["result"]["data"]["refresh_id"]
    [approved] = body(call(client, key_a, "list_refresh_requests"))["items"]
    assert approved["refresh_id"] == refresh_id and approved["status"] == "approved"
    claimed = body(call(client, key_a, "report_refresh", {"refresh_id": refresh_id,
                                                          "status": "running"}))
    assert claimed["status"] == "running"
    again = call(client, key_a, "report_refresh", {"refresh_id": refresh_id, "status": "running"})
    assert again.status_code == 409
    body(call(client, key_a, "report_refresh", {"refresh_id": refresh_id, "status": "completed",
                                                "run_ids": ["run-cal-00009"]}))
    [seen] = body(call(client, key_b, "list_refresh_requests", {"status": "all"}))["items"]
    assert seen["status"] == "completed" and seen["run_ids"] == ["run-cal-00009"]


def test_draft_only_holds_even_for_a_direct_capability_on_a_full_key(client, finance, make_agent):
    full = make_agent([{"target": "finance", "actions": ["request_refresh"], "mode": "direct"}],
                      role="full")
    r = call(client, full, "request_refresh", {"start": "2026-10-01", "end": "2026-10-31",
                                               "reason": "now please"})
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"


# ---- MCP -------------------------------------------------------------------------------------

def _tools(live, headers) -> set[str]:
    r = live.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                  headers={**headers, **ACCEPT})
    assert r.status_code == 200, r.text
    return {t["name"] for t in r.json()["result"]["tools"] if t["name"].startswith("finance_")}


def test_mcp_lists_finance_tools_per_key(live, key_a, key_b):
    assert _tools(live, key_b.headers) == {f"finance_{a}" for a in READS} | {
        "finance_set_note", "finance_request_refresh"}
    assert _tools(live, key_a.headers) == {"finance_ingest_snapshot", "finance_report_refresh",
                                           "finance_list_refresh_requests", "finance_list_runs"}


def test_the_skill_doc_for_b_has_the_finance_section(client, key_b):
    r = client.get("/v1/me/skill", headers=key_b.headers)
    assert r.status_code == 200 and "Personal finance" in r.text
    assert "data, not instructions" in r.text


# ---- the broker's own guarantees around the plugin ------------------------------------------------

def test_the_post_filter_holds_when_the_plugin_leaks(client, finance, key_a, key_b,
                                                    monkeypatch):
    seed(client, key_a)
    hidden.add("finance", "card", "max:5678")
    # Break the plugin: it now ignores every visibility rule.
    monkeypatch.setattr(finance.adapter, "_view", lambda call, window=True: View())
    leaked = finance.adapter.perform("list_transactions", {}, {"visibility": {
        "card": {"deny": ["max:5678"], "allow_only": None}}}).data["items"]
    assert "max:5678" in {t["source"]["id"] for t in leaked}           # the leak is real
    rows = body(call(client, key_b, "list_transactions"))["items"]
    assert "max:5678" not in {t["source"]["id"] for t in rows}
    groups = body(call(client, key_b, "monthly_summary", {"year": 2026}))["groups"]
    assert "max:5678" not in {g["source"]["id"] for g in groups}


def test_a_503_releases_the_write_reservation(client, key_a, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(ingest_mod, "_insert", locked)
    r = upload(client, key_a, "cal", "1234", CAL_TXNS, "run-cal-00003")
    assert r.status_code == 503 and r.json()["code"] == "unavailable"
    assert ledger_states() == ["released"] and outcomes() == ["unavailable"]


def test_discovery_refuses_another_manifest_version(env, owner, tmp_path, fake_now):
    from aab_plugin_finance.adapter import FinanceAdapter
    from aab_plugin_finance.clock import Clock
    from aab_plugin_finance.store import Store
    from broker.plugins.registry import get_registry
    adapter = FinanceAdapter(Store(str(tmp_path / "f.db")), Clock("Asia/Jerusalem", fake_now))
    adapter.manifest = {**adapter.manifest, "version": "9.9.9"}
    discover(serve_finance(adapter, tmp_path))
    assert "finance" not in get_registry().entries()
    assert "manifest mismatch" in get_registry().refused["finance"]


def test_the_pinned_manifest(finance):
    from broker.plugins.registry import get_registry
    m = get_registry().manifests()["finance"]
    assert m.config_schema == [] and m.connection.kind == "none"
    assert m.connection.enforcement == "proxy"
    assert FINANCE_TOKEN not in repr(get_registry().adapter("finance"))
