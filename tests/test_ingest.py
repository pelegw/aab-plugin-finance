"""ingest_snapshot: chunking, idempotency, validation, the merge, failure atomicity."""

import sqlite3

import pytest

from aab_plugin_finance import ingest as ingest_mod

from .conftest import data, items, scope
from .fakes import (CAL_TXNS, WIDE, chunk_params, ingest, ingest_rows, make_rows, seed,
                    txn)


def rows_for(n: int, *, month: str = "2026-09", status: str = "completed") -> list[dict]:
    return ingest_rows(make_rows("cal", "1234", [
        txn(f"{month}-{(i % 28) + 1:02d}", -(i + 1), f"SHOP {i}", status=status)
        for i in range(n)], WIDE))


def send(perform, rows, *, index, total, run_id="run-0000000001", company="cal",
         source_id="cal:1234", call_scope=None, **kw):
    return perform("ingest_snapshot", chunk_params(company, source_id, rows, index=index,
                                                   total=total, run_id=run_id, **kw), call_scope)


def tx_count(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


# ---- chunks and idempotency -----------------------------------------------------------

def test_a_single_chunk_applies_at_once(perform):
    answer = ingest(perform, "cal", "1234", CAL_TXNS)
    assert answer == {"status": "applied", "run_id": answer["run_id"], "added": 3,
                      "updated": 0, "unchanged": 0, "removed_pending": 0}


def test_chunks_apply_in_any_order(perform, db_path):
    rows = rows_for(5)
    parts = [rows[0:2], rows[2:4], rows[4:5]]
    first = data(send(perform, parts[2], index=3, total=3))
    assert first == {"status": "staged", "run_id": "run-0000000001", "received": [3],
                     "missing": [1, 2]}
    assert tx_count(db_path) == 0                       # nothing applied while staging
    assert data(send(perform, parts[0], index=1, total=3))["missing"] == [2]
    last = data(send(perform, parts[1], index=2, total=3))
    assert last["status"] == "applied" and last["added"] == 5
    assert tx_count(db_path) == 5


def test_a_resent_chunk_is_a_duplicate(perform):
    rows = rows_for(4)
    send(perform, rows[:2], index=1, total=2)
    again = data(send(perform, rows[:2], index=1, total=2))
    assert again == {"status": "duplicate", "run_id": "run-0000000001", "received": [1],
                     "missing": [2]}


def test_same_index_with_other_content_is_409(perform):
    rows = rows_for(4)
    send(perform, rows[:2], index=1, total=2)
    r = send(perform, rows[1:3], index=1, total=2)
    assert r.status_code == 409


def test_a_chunk_after_the_apply_answers_applied_again(perform, db_path):
    rows = rows_for(3)
    applied = data(send(perform, rows, index=1, total=1))
    replay = data(send(perform, rows, index=1, total=1))
    assert replay == applied and tx_count(db_path) == 3


def test_a_chunk_that_contradicts_its_run_is_409(perform):
    rows = rows_for(4)
    send(perform, rows[:2], index=1, total=2)
    r = send(perform, rows[2:], index=2, total=2, rng=("2026-01-01", "2026-12-31"))
    assert r.status_code == 409


# ---- validation (400 before any write) ---------------------------------------------------

def test_a_row_outside_the_range_is_400_and_nothing_is_staged(perform, db_path):
    rows = rows_for(2) + ingest_rows(make_rows("cal", "1234", [txn("2027-01-01", -1, "LATE")]))
    r = send(perform, rows, index=1, total=1)
    assert r.status_code == 400 and "transactions[2]" in r.json()["error"]
    assert "2027-01-01" not in r.text and "LATE" not in r.text       # never echoed
    assert items(perform("list_runs")) == [] and tx_count(db_path) == 0


def test_more_than_500_rows_is_400(perform):
    r = send(perform, rows_for(501), index=1, total=1)
    assert r.status_code == 400 and "500" in r.json()["error"]


@pytest.mark.parametrize("change,where", [
    ({"date": "2026-02-30"}, "date"),
    ({"date": "2026-9-01"}, "date"),
    ({"original_x100": True}, "original_x100"),
    ({"charged_x100": 1.5}, "charged_x100"),
    ({"charged_x100": 10 ** 13 + 1}, "charged_x100"),
    ({"status": "settled"}, "status"),
    ({"original_currency": ""}, "original_currency"),
    ({"installment_total": 0}, "installment_total"),
    ({"installment_total": 10 ** 30}, "installment_total"),
    ({"installment_number": 10 ** 30}, "installment_number"),
    ({"surprise": 1}, "unknown"),
    ({"key": ""}, "key"),
])
def test_bad_rows_are_400(perform, change, where):
    rows = rows_for(1)
    rows[0].update(change)
    r = send(perform, rows, index=1, total=1)
    assert r.status_code == 400 and where in r.json()["error"], r.text


@pytest.mark.parametrize("params_change", [
    {"run_id": "bad id!"},
    {"run_id": "short"},
    {"company": "Cal Ltd"},
    {"source": {"kind": "card", "id": "max:5678"}},          # not under the run's company
    {"source": {"kind": "card", "id": "cal1234"}},
    {"source": {"kind": "wallet", "id": "cal:1234"}},
    {"range": {"start": "2026-10-01", "end": "2026-09-01"}},
    {"chunk": {"index": 3, "total": 2}},
    {"refresh_id": "rr 1"},
    {"extra": True},
])
def test_bad_run_params_are_400(perform, params_change):
    params = chunk_params("cal", "cal:1234", rows_for(1), index=1, total=1,
                          run_id="run-0000000001")
    params.update(params_change)
    assert perform("ingest_snapshot", params).status_code == 400


def test_the_source_id_is_normalized(perform):
    r = send(perform, rows_for(1), index=1, total=1, source_id="CAL 1234")
    assert r.status_code == 200
    assert [s["id"] for s in items(perform("list_sources"))] == ["cal:1234"]


# ---- the merge ---------------------------------------------------------------------------

def test_re_upload_counts_updated_and_unchanged_and_keeps_first_seen(perform, fake_now):
    ingest(perform, "cal", "1234", CAL_TXNS, scraped_at="2026-10-01T06:00:00Z")
    changed = [dict(t) for t in CAL_TXNS]
    changed[1]["memo"] = "new memo"
    fake_now.advance(86400)
    answer = ingest(perform, "cal", "1234", changed, scraped_at="2026-10-06T06:00:00Z")
    assert (answer["added"], answer["updated"], answer["unchanged"]) == (0, 1, 2)
    netflix = [t for t in items(perform("list_transactions")) if t["description"] == "NETFLIX"]
    assert netflix[0]["memo"] == "new memo"
    assert (netflix[0]["first_seen"], netflix[0]["last_seen"]) == ("2026-10-01", "2026-10-06")


def test_first_seen_is_the_scrapes_local_date(perform):
    # 22:30 UTC on the 5th is already the 6th in Jerusalem.
    ingest(perform, "cal", "1234", CAL_TXNS[:1], scraped_at="2026-10-05T22:30:00.000Z")
    assert items(perform("list_transactions"))[0]["first_seen"] == "2026-10-06"


def test_stale_pending_rows_go_only_inside_the_range(perform):
    ingest(perform, "cal", "1234", [
        txn("2026-09-10", -10, "PENDING IN RANGE", status="pending"),
        txn("2026-08-10", -20, "PENDING BEFORE RANGE", status="pending"),
        txn("2026-09-11", -30, "SETTLED NOT RESENT"),
        txn("2026-09-12", -40, "PENDING RESENT", status="pending"),
    ])
    answer = ingest(perform, "cal", "1234", [txn("2026-09-12", -40, "PENDING RESENT",
                                                 status="pending")],
                    rng=("2026-09-01", "2026-09-30"))
    assert answer["removed_pending"] == 1
    left = {t["description"] for t in items(perform("list_transactions"))}
    assert left == {"PENDING BEFORE RANGE", "SETTLED NOT RESENT", "PENDING RESENT"}


def test_an_empty_run_still_clears_stale_pending(perform):
    ingest(perform, "cal", "1234", [txn("2026-09-10", -10, "GONE", status="pending")])
    answer = ingest(perform, "cal", "1234", [], rng=("2026-09-01", "2026-09-30"))
    assert answer["removed_pending"] == 1 and items(perform("list_transactions")) == []


def test_derived_columns(perform, db_path):
    ingest(perform, "cal", "1234", [
        txn("2026-09-01", -10, "  Bit   Payment  "), txn("2026-09-02", -55, "HOTEL",
                                                         currency="EUR", charged=-200),
        txn("2026-09-03", -70, "ביטוח רכב"), txn("2026-09-04", -1, "שח", currency="₪")])
    with sqlite3.connect(db_path) as conn:
        got = {r[0]: r[1:] for r in conn.execute(
            "SELECT description, merchant_key, is_transfer, is_foreign FROM transactions")}
    assert got["Bit Payment"] == ("bit payment", 1, 0)
    assert got["HOTEL"] == ("hotel", 0, 1)
    assert got["ביטוח רכב"] == ("ביטוח רכב", 0, 0)                # insurance, not Bit
    assert got["שח"][2] == 0                                       # ₪ normalized to ILS


def test_a_key_owned_by_another_source_is_409(perform):
    rows = rows_for(1)
    send(perform, rows, index=1, total=1)
    r = perform("ingest_snapshot", chunk_params("cal", "cal:9999", rows, index=1, total=1,
                                                run_id="run-0000000002"))
    assert r.status_code == 409


def test_a_card_cannot_become_an_account(perform):
    send(perform, rows_for(1), index=1, total=1)
    r = perform("ingest_snapshot", chunk_params("cal", "cal:1234", [], index=1, total=1,
                                                run_id="run-0000000002", kind="account"))
    assert r.status_code == 409


def test_source_details_are_kept_when_an_upload_omits_them(perform):
    perform("ingest_snapshot", chunk_params("leumi", "leumi:123456", [], index=1, total=1,
                                            run_id="run-0000000001", kind="account",
                                            label="Main account", balance_x100=1234567,
                                            balance_at="2026-10-05"))
    perform("ingest_snapshot", chunk_params("leumi", "leumi:123456", [], index=1, total=1,
                                            run_id="run-0000000002", kind="account"))
    [src] = items(perform("list_sources"))
    assert (src["label"], src["balance"], src["balance_at"]) == ("Main account", 12345.67,
                                                                 "2026-10-05")


# ---- scope: company-bound, card visibility ignored -----------------------------------------

def test_a_hidden_card_does_not_block_its_upload(perform):
    hidden = scope(card_deny=["cal:1234"])
    assert ingest(perform, "cal", "1234", CAL_TXNS, call_scope=hidden)["added"] == 3
    # ...while reads of it stay hidden.
    assert items(perform("list_transactions", {}, hidden)) == []


def test_the_company_visibility_applies(perform):
    outside = send(perform, rows_for(1), index=1, total=1,
                   call_scope=scope(company_allow=["max"]))
    denied = send(perform, rows_for(1), index=1, total=1, call_scope=scope(company_deny=["cal"]))
    assert outside.status_code == 403 and denied.status_code == 404


# ---- failures and the abandon sweep -------------------------------------------------------------

def test_a_store_failure_mid_apply_is_503_and_applies_nothing(perform, db_path, monkeypatch):
    seed(perform)
    before = tx_count(db_path)
    calls = {"n": 0}
    real = ingest_mod._insert

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:                      # after two rows went in
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest_mod, "_insert", flaky)
    r = send(perform, rows_for(5), index=1, total=1, run_id="run-0000000009")
    assert r.status_code == 503
    assert tx_count(db_path) == before                         # no partial apply
    assert "run-0000000009" not in {x["run_id"] for x in items(perform("list_runs"))}
    monkeypatch.setattr(ingest_mod, "_insert", real)
    retry = data(send(perform, rows_for(5), index=1, total=1, run_id="run-0000000009"))
    assert retry["status"] == "applied" and retry["added"] == 5


def test_an_unexpected_failure_before_commit_is_503_too(perform, db_path, monkeypatch):
    def boom(*args, **kwargs):
        raise KeyError("bug")
    monkeypatch.setattr(ingest_mod, "_upsert_source", boom)
    r = send(perform, rows_for(2), index=1, total=1)
    assert r.status_code == 503 and "bug" not in r.text and tx_count(db_path) == 0


def test_a_run_left_staging_for_a_day_is_abandoned(perform, fake_now):
    rows = rows_for(4)
    send(perform, rows[:2], index=1, total=2)
    fake_now.advance(24 * 3600 + 1)
    [run] = items(perform("list_runs"))
    assert run["status"] == "abandoned" and run["chunks_received"] == 0
    r = send(perform, rows[2:], index=2, total=2)
    assert r.status_code == 409


def test_runs_record_the_refresh_id_and_scrape_time(perform):
    ingest(perform, "cal", "1234", CAL_TXNS, refresh_id="rr_0123456789abcdef",
           run_id="run-0000000042")
    [run] = items(perform("list_runs"))
    assert run["refresh_id"] == "rr_0123456789abcdef" and run["run_id"] == "run-0000000042"
    assert run["scraped_at"] and run["range"] == {"start": WIDE[0], "end": WIDE[1]}
    assert run["resource_ref"] == {"kind": "card", "id": "cal:1234"}
