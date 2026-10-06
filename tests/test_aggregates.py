"""Aggregates against numbers calculated by hand from cred-analysis analysis.js.

The fixture has 26 rows on two cards, with today = 2026-10-06. It gives every
heuristic a case on each side of its line. The expected values below come
from a hand calculation with the rules of analysis.js, and the comments show
the sums. The code under test calculates none of them. An optional
cross-check runs the real analysis.js under Node on the same rows, when Node
and a cred-analysis checkout are available.

Rows (charged ILS; negative = purchase):
  cal:1111  SPOTIFY -19.90 x4 (Jun-Sep, one spelled "Spotify")    Streaming
            SUPERMARKET -300 Jul, -250 Aug, -350 Sep, +50 Sep     Groceries
            TV STORE -400 Aug (1/3), -400 Sep (2/3), deal 1200     Electronics
            ביט העברה -100 Sep (a transfer)                        (none)
            CAFE -25 Oct, pending                                  Food
  max:2222  HOTEL PARIS -400 Jul (100 EUR), -210 Aug (50 EUR)      Travel
            SUPERMARKET -150 Sep                                   Groceries
            GYM -150 x5 (Jun, Jul, Aug, Sep twice)                 Sport
            OLD SUB -10 x4 (Mar-Jun)                               Other
            PHONE -300 May (1/2), -300 Jun (2/2), deal 600         Electronics
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import data, items, scope
from .fakes import ingest, make_rows, txn

CAL, MAX = "cal:1111", "max:2222"
CAL_TXNS = (
    [txn(f"2026-0{m}-05", -19.90, "Spotify" if m == 8 else "SPOTIFY", category="Streaming")
     for m in (6, 7, 8, 9)]
    + [txn("2026-07-10", -300, "SUPERMARKET", category="Groceries"),
       txn("2026-08-12", -250, "SUPERMARKET", category="Groceries"),
       txn("2026-09-14", -350, "SUPERMARKET", category="Groceries"),
       txn("2026-09-20", 50, "SUPERMARKET", category="Groceries"),
       txn("2026-08-01", -1200, "TV STORE", charged=-400, installments=(1, 3),
           category="Electronics"),
       txn("2026-09-01", -1200, "TV STORE", charged=-400, installments=(2, 3),
           category="Electronics"),
       txn("2026-09-25", -100, "ביט העברה"),
       txn("2026-10-03", -25, "CAFE", status="pending", category="Food")])
MAX_TXNS = (
    [txn("2026-07-15", -100, "HOTEL PARIS", currency="EUR", charged=-400, category="Travel"),
     txn("2026-08-16", -50, "HOTEL PARIS", currency="EUR", charged=-210, category="Travel"),
     txn("2026-09-17", -150, "SUPERMARKET", category="Groceries")]
    + [txn(d, -150, "GYM", category="Sport")
       for d in ("2026-06-20", "2026-07-20", "2026-08-20", "2026-09-20", "2026-09-28")]
    + [txn(f"2026-0{m}-03", -10, "OLD SUB", category="Other") for m in (3, 4, 5, 6)]
    + [txn("2026-05-10", -600, "PHONE", charged=-300, installments=(1, 2),
           category="Electronics"),
       txn("2026-06-10", -600, "PHONE", charged=-300, installments=(2, 2),
           category="Electronics")])


@pytest.fixture()
def fixture(perform):
    ingest(perform, "cal", "1111", CAL_TXNS)
    ingest(perform, "max", "2222", MAX_TXNS)
    return perform


# ---- monthly_summary -------------------------------------------------------------------------

def test_year_totals(fixture):
    t = data(fixture("monthly_summary", {"year": 2026}))["totals"]
    assert t == {
        "count": 26,
        "spend": 4054.6, "refunds": 50.0,
        "net": 4004.6,               # cal 1854.60 + max 2150.00
        "months_with_data": 8,       # Mar..Oct
        "avg_per_month": 500.58,     # 4004.60 / 8 = 500.575
        "pending_count": 1, "pending_net": 25.0,
        "transfer_net": 100.0, "consumption_net": 3904.6,
        "foreign_net": 610.0, "foreign_share": 0.1523,          # 610 / 4004.60
        "avg_ticket": 162.18,        # 4054.60 / 25 purchases = 162.184
    }


def test_year_months_include_empty_ones_and_change(fixture):
    months = data(fixture("monthly_summary", {"year": 2026}))["months"]
    assert [m["month"] for m in months] == [f"2026-{m:02d}" for m in range(1, 11)]
    assert [(m["count"], m["net"], m["change"]) for m in months] == [
        (0, 0.0, None), (0, 0.0, None),           # Jan, Feb: no data
        (1, 10.0, None),                          # previous net 0: no change
        (1, 10.0, 0.0),
        (2, 310.0, 30.0),                         # (310 - 10) / 10
        (4, 479.9, 0.5481),                       # 169.90 / 310
        (4, 869.9, 0.8127),                       # 390 / 479.90
        (5, 1029.9, 0.1839),                      # 160 / 869.90
        (8, 1269.9, 0.233),                       # 240 / 1029.90
        (1, 25.0, -0.9803),                       # -1244.90 / 1269.90
    ]


def test_by_source_groups(fixture):
    out = data(fixture("monthly_summary", {"year": 2026}))
    assert [(g["source"]["id"], g["count"], g["net"], g["share"]) for g in out["groups"]] == [
        (MAX, 14, 2150.0, 0.5369), (CAL, 12, 1854.6, 0.4631)]
    sept = next(m for m in out["months"] if m["month"] == "2026-09")
    cells = {g["source"]["id"]: (g["count"], g["spend"], g["refunds"], g["net"])
             for g in sept["groups"]}
    assert cells == {CAL: (5, 869.9, 50.0, 819.9), MAX: (3, 450.0, 0.0, 450.0)}


def test_category_groups_and_a_custom_range(fixture):
    out = data(fixture("monthly_summary", {"start": "2026-09-01", "end": "2026-09-30",
                                           "group_by": "category"}))
    assert [m["month"] for m in out["months"]] == ["2026-09"]      # no year: data months only
    nets = {g["category"]: g["net"] for g in out["groups"]}
    assert nets == {"Groceries": 450.0, "Electronics": 400.0, "Sport": 300.0,
                    "(uncategorized)": 100.0, "Streaming": 19.9}


# ---- categories, merchants -----------------------------------------------------------------

def test_by_category(fixture):
    out = data(fixture("by_category"))
    assert out["net"] == 4004.6
    assert [(c["category"], c["count"], c["net"], c["share"]) for c in out["items"]] == [
        ("Electronics", 4, 1400.0, 0.3496), ("Groceries", 5, 1000.0, 0.2497),
        ("Sport", 5, 750.0, 0.1873), ("Travel", 2, 610.0, 0.1523),
        ("(uncategorized)", 1, 100.0, 0.025), ("Streaming", 4, 79.6, 0.0199),
        ("Other", 4, 40.0, 0.01), ("Food", 1, 25.0, 0.0062)]


def test_top_merchants(fixture):
    top = items(fixture("top_merchants", {"limit": 3}))
    assert [(m["merchant"], m["net"]) for m in top] == [
        ("SUPERMARKET", 1000.0), ("TV STORE", 800.0), ("GYM", 750.0)]
    s = top[0]
    assert (s["count"], s["refunds"], s["months"], s["sources"]) == (5, 50.0, 3, [CAL, MAX])
    assert s["avg_per_month"] == 333.33          # 1000 / 3
    assert s["avg_amount"] == 262.5              # (300 + 250 + 350 + 150) / 4 purchases
    assert s["last_date"] == "2026-09-20" and s["transfer"] is False
    everyone = [m["merchant"] for m in items(fixture("top_merchants"))]
    assert everyone == ["SUPERMARKET", "TV STORE", "GYM", "HOTEL PARIS", "PHONE",
                        "ביט העברה", "SPOTIFY", "OLD SUB", "CAFE"]


def test_recurring_merchants(fixture):
    # 3+ distinct months and net > 0.
    assert [(m["merchant"], m["months"]) for m in items(fixture("recurring_merchants"))] == [
        ("SUPERMARKET", 3), ("GYM", 4), ("SPOTIFY", 4), ("OLD SUB", 4)]


def test_subscriptions(fixture):
    subs = items(fixture("subscriptions"))
    # SUPERMARKET: 3 months only. GYM: 5 charges in 4 months is still <= months + 1.
    assert [(s["merchant"], s["monthly_amount"], s["active"], s["cv"]) for s in subs] == [
        ("GYM", 150.0, True, 0.0),
        ("SPOTIFY", 19.9, True, 0.0),            # last 2026-09-05 >= "2026-09"
        ("OLD SUB", 10.0, False, 0.0),           # last 2026-06-03
    ]


@pytest.mark.parametrize("amounts,dates,expected", [
    # cv: mean 125, deviations -25 -25 -25 +75 -> std 43.3 -> cv 0.346 > 0.15
    ([100, 100, 100, 200], ["06", "07", "08", "09"], False),
    # cv: 100 100 100 115 -> mean 103.75, std 6.50 -> cv 0.0626 <= 0.15
    ([100, 100, 100, 115], ["06", "07", "08", "09"], True),
    # 6 charges in 4 months > months + 1
    ([100] * 6, ["06", "07", "08", "09", "09", "09"], False),
    # only 3 months
    ([100] * 3, ["07", "08", "09"], False),
])
def test_subscription_edges(perform, amounts, dates, expected):
    ingest(perform, "cal", "1111", [txn(f"2026-{m}-{i + 1:02d}", -a, "UTILITY")
                                    for i, (a, m) in enumerate(zip(amounts, dates))])
    assert bool(items(perform("subscriptions"))) is expected


def test_transfers_and_installments_are_never_subscriptions(perform):
    ingest(perform, "cal", "1111",
           [txn(f"2026-0{m}-01", -50, "העברה לחיסכון") for m in (5, 6, 7, 8)]
           + [txn(f"2026-0{m}-02", -50, "LOAN", installments=(m - 4, 12)) for m in (5, 6, 7, 8)])
    assert items(perform("subscriptions")) == []
    assert {m["merchant"] for m in items(perform("recurring_merchants"))} == {
        "העברה לחיסכון", "LOAN"}


# ---- installments, foreign, largest ---------------------------------------------------------

def test_installments(fixture):
    out = data(fixture("installments"))
    assert {k: out[k] for k in ("active_count", "completed_count", "remaining_total",
                                "monthly_commitment", "paid_this_period")} == {
        "active_count": 1, "completed_count": 1,
        "remaining_total": 400.0,      # TV STORE: 1 of 3 left at 400
        "monthly_commitment": 400.0,
        "paid_this_period": 1400.0,    # TV 800 + PHONE 600
    }
    [tv] = out["items"]
    assert {k: tv[k] for k in ("description", "total", "deal_amount", "paid", "remaining",
                               "per_installment", "remaining_amount", "first_date",
                               "last_date")} == {
        "description": "TV STORE", "total": 3, "deal_amount": 1200.0, "paid": 2,
        "remaining": 1, "per_installment": 400.0, "remaining_amount": 400.0,
        "first_date": "2026-08-01", "last_date": "2026-09-01"}
    assert tv["source"] == {"kind": "card", "id": CAL}
    every = items(fixture("installments", {"active_only": False}))
    assert [(p["description"], p["remaining"]) for p in every] == [("TV STORE", 1), ("PHONE", 0)]


def test_foreign_currency(fixture):
    out = data(fixture("foreign_currency"))
    assert out == {"foreign_net": 610.0, "items": [{
        "currency": "EUR", "count": 2, "original_total": 150.0, "charged_total": 610.0,
        "implied_rate": 4.0667,                       # 610 / 150
        "months": ["2026-07", "2026-08"]}]}


def test_largest_purchases(fixture):
    top = items(fixture("largest_purchases", {"limit": 4}))
    assert [(t["description"], t["date"], t["charged_amount"]) for t in top] == [
        ("TV STORE", "2026-09-01", -400.0), ("TV STORE", "2026-08-01", -400.0),
        ("HOTEL PARIS", "2026-07-15", -400.0), ("SUPERMARKET", "2026-09-14", -350.0)]


def test_source_and_range_filters(fixture):
    assert data(fixture("by_category", {"source": MAX}))["net"] == 2150.0
    assert data(fixture("by_category", {"start": "2026-09-01", "end": "2026-09-30"}))["net"] \
        == 1269.9
    assert fixture("by_category", {"start": "2026-10-01", "end": "2026-09-01"}).status_code == 400


def test_year_and_range_are_exclusive(fixture):
    r = fixture("monthly_summary", {"year": 2026, "start": "2026-01-01"})
    assert r.status_code == 400


def test_aggregates_respect_the_window(fixture):
    window = scope(date_window_days=5)            # from 2026-10-01: only CAFE
    assert data(fixture("monthly_summary", {}, window))["totals"]["net"] == 25.0
    assert [m["merchant"] for m in items(fixture("top_merchants", {}, window))] == ["CAFE"]
    wider = scope(date_window_days=10)            # from 2026-09-26: CAFE and GYM on 09-28
    assert data(fixture("monthly_summary", {}, wider))["totals"]["net"] == 175.0


# ---- optional: the real analysis.js under Node -------------------------------------------------

def _cred_analysis() -> Path | None:
    root = Path(os.environ.get("CRED_ANALYSIS_SRC")
                or Path(__file__).resolve().parents[2] / "cred-analysis")
    ok = (root / "src" / "analysis.js").is_file() and (root / "node_modules").is_dir()
    return root if ok and shutil.which("node") else None


@pytest.mark.skipif(_cred_analysis() is None, reason="needs node and a cred-analysis checkout")
def test_numbers_match_analysis_js(fixture, tmp_path):
    root = _cred_analysis()
    rows = make_rows("cal", "1111", CAL_TXNS) + make_rows("max", "2222", MAX_TXNS)
    (tmp_path / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
    script = (
        "import fs from 'node:fs';"
        f"const {{ analyze }} = await import({json.dumps((root / 'src' / 'analysis.js').as_uri())});"
        "const rows = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));"
        "const r = analyze(rows, { year: 2026, top: 25 });"
        "const pick = (m) => [m.name, Math.round(m.net * 100)];"
        "console.log(JSON.stringify({net: Math.round(r.totals.net * 100), count: r.totals.count,"
        " top: r.topMerchants.map(pick), recurring: r.recurring.map(pick),"
        " subs: r.subscriptions.map((s) => [s.name, Math.round(s.monthlyAmount * 100)]),"
        " cats: r.byCategory.map((c) => [c.name, Math.round(c.net * 100)]),"
        " remaining: Math.round(r.installments.remainingTotal * 100),"
        " commitment: Math.round(r.installments.monthlyCommitment * 100),"
        " foreign: r.foreign.map((f) => [f.currency, Math.round(f.chargedTotal * 100)])}));")
    out = subprocess.run(["node", "--input-type=module", "-e", script, str(tmp_path / "rows.json")],
                         capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=root)
    assert out.returncode == 0, out.stderr[-2000:]
    js = json.loads(out.stdout.strip().splitlines()[-1])

    def x100(v):
        return round(v * 100)

    summary = data(fixture("monthly_summary", {"year": 2026}))
    assert (x100(summary["totals"]["net"]), summary["totals"]["count"]) == (js["net"], js["count"])
    assert [[m["merchant"], x100(m["net"])] for m in items(fixture("top_merchants"))] == js["top"]
    assert [[m["merchant"], x100(m["net"])]
            for m in items(fixture("recurring_merchants"))] == js["recurring"]
    assert [[s["merchant"], x100(s["monthly_amount"])]
            for s in items(fixture("subscriptions"))] == js["subs"]
    assert [[c["category"], x100(c["net"])] for c in items(fixture("by_category"))] == js["cats"]
    plans = data(fixture("installments"))
    assert (x100(plans["remaining_total"]), x100(plans["monthly_commitment"])) == (
        js["remaining"], js["commitment"])
    assert [[f["currency"], x100(f["charged_total"])]
            for f in items(fixture("foreign_currency"))] == js["foreign"]


def test_large_amounts_do_not_overflow_the_merchant_aggregates(perform):
    # Two 22 million ILS purchases at one merchant: as integers their squares
    # pass 2^63, SQLite's SUM raises, and every key would get a 503 from the
    # three merchant aggregates until the rows changed. The squares are REAL.
    big = [txn("2026-08-01", -22_000_000, "YACHT"), txn("2026-09-01", -22_000_000, "YACHT")]
    ingest(perform, "cal", "1111", big)
    for action in ("top_merchants", "recurring_merchants", "subscriptions"):
        assert perform(action).status_code == 200, action
    [m] = [m for m in items(perform("top_merchants")) if m["merchant"] == "YACHT"]
    assert m["count"] == 2
