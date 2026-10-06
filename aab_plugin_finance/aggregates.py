"""Aggregates: a port of cred-analysis `src/analysis.js`.

Each function is a GROUP BY over `store.visible_tx` (the same visibility-
filtered subquery line items use), so a hidden card, a hidden transaction or
a row outside the date window can never move a total. SQL does the grouping
and the integer sums; Python finishes what analysis.js computes per group
(coefficient of variation, implied rates, change against the previous
month, the year's empty months) and turns hundredths into units at the end.

The heuristics are analysis.js's, verbatim (sign convention: a negative
charged amount is a purchase; "net" is purchases minus refunds, positive):
  spend      -charged when charged < 0;   refunds   charged when charged > 0
  months     a year view lists Jan..the current month even without data
  change     (net - previous net) / previous net, null when previous is 0
  top        merchants with net > 0, by net
  recurring  3+ distinct months and net > 0
  subscriptions  4+ months, net > 0, not a transfer, no installments,
             purchases <= months + 1, cv(purchase amounts) <= 0.15; active
             when last charged on or after the first day of last month
             (analysis.js compares "YYYY-MM-DD" >= "YYYY-MM", same here)
  installments   plans grouped by card, merchant, plan length and deal
             amount; paid = highest installment number seen (else the
             charges counted); remaining = total - paid
  foreign    original currency outside {'', ILS, NIS, ₪}; implied rate =
             charged / original
Groups that analysis.js keys by workbook sheet ("Cal 1234") are keyed by
source id ("cal:1234") here: the same card, its canonical id.
"""

from dataclasses import replace

from . import rows
from .clock import Clock
from .heuristics import cv_from_sums
from .rows import amount, ratio
from .scope import View
from .store import Filters, visible_tx

_SPEND = "SUM(CASE WHEN charged_x100 < 0 THEN -charged_x100 ELSE 0 END)"
_REFUNDS = "SUM(CASE WHEN charged_x100 > 0 THEN charged_x100 ELSE 0 END)"
_NET = "SUM(-charged_x100)"
UNCATEGORIZED = "(uncategorized)"


def _months_for(data_months: set[str], year: int | None, clock: Clock) -> list[str]:
    """analysis.js monthsFor: every month with data, plus Jan..the current
    month for the current year (all twelve for a past year, none ahead)."""
    months = set(data_months)
    if year is not None:
        today = clock.today()
        last = 12 if year < today.year else today.month if year == today.year else 0
        months.update(f"{year}-{m:02d}" for m in range(1, last + 1))
    return sorted(months)


def _money(d: dict, *names: str) -> dict:
    return {n: amount(d[n]) for n in names}


def monthly_summary(conn, view: View, filters: Filters, group_by: str, year: int | None,
                    clock: Clock) -> dict:
    params: list = []
    group = {"source": "source_id", "category":
             f"CASE WHEN category = '' THEN '{UNCATEGORIZED}' ELSE category END",
             "none": "''"}[group_by]
    sql = (f"WITH v AS ({visible_tx(view, filters, params)})"
           f" SELECT strftime('%Y-%m', date) AS month, {group} AS grp,"
           " MAX(source_kind) AS kind, COUNT(*) AS count,"
           f" {_SPEND} AS spend, {_REFUNDS} AS refunds, {_NET} AS net,"
           " SUM(CASE WHEN charged_x100 < 0 THEN 1 ELSE 0 END) AS purchases,"
           " SUM(CASE WHEN is_foreign = 1 THEN -charged_x100 ELSE 0 END) AS foreign_net,"
           " SUM(CASE WHEN is_transfer = 1 THEN -charged_x100 ELSE 0 END) AS transfer_net,"
           " SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,"
           " SUM(CASE WHEN status = 'pending' THEN -charged_x100 ELSE 0 END) AS pending_net,"
           " MIN(date) AS first, MAX(date) AS last"
           " FROM v GROUP BY month, grp ORDER BY month, grp")
    cells = [dict(r) for r in conn.execute(sql, params)]
    sums = ("count", "spend", "refunds", "net", "purchases", "foreign_net", "transfer_net",
            "pending_count", "pending_net")
    months_out, by_month, groups = [], {}, {}
    for c in cells:
        m = by_month.setdefault(c["month"], {k: 0 for k in sums} | {"groups": []})
        for k in sums:
            m[k] += c[k]
        if group_by != "none":
            m["groups"].append(_group_cell(c, group_by))
            g = groups.setdefault(c["grp"], {k: 0 for k in sums} | {
                "grp": c["grp"], "kind": c["kind"], "first": c["first"], "last": c["last"]})
            for k in sums:
                g[k] += c[k]
            g["first"], g["last"] = min(g["first"], c["first"]), max(g["last"], c["last"])
    prev = None
    for month in _months_for(set(by_month), year, clock):
        m = by_month.get(month, {k: 0 for k in sums} | {"groups": []})
        change = (m["net"] - prev) / prev if prev else None
        months_out.append({
            "month": month, "count": m["count"], **_money(m, "spend", "refunds", "net"),
            "change": ratio(change),
            "avg_ticket": amount(round(m["spend"] / m["purchases"])) if m["purchases"] else 0.0,
            **_money(m, "foreign_net", "transfer_net"), "groups": m["groups"]})
        prev = m["net"]
    total = {k: sum(by_month[m][k] for m in by_month) for k in sums}
    current = clock.current_month()
    with_data = len([m for m in by_month if m <= current])
    out = {
        "start": filters.start, "end": filters.end, "group_by": group_by, "months": months_out,
        "groups": [_group_total(g, group_by, total["net"])
                   for g in sorted(groups.values(), key=lambda g: (-g["net"], g["grp"]))],
        "totals": {
            "count": total["count"], **_money(total, "spend", "refunds", "net"),
            "months_with_data": with_data,
            "avg_per_month": amount(round(total["net"] / with_data)) if with_data else 0.0,
            "pending_count": total["pending_count"], "pending_net": amount(total["pending_net"]),
            "transfer_net": amount(total["transfer_net"]),
            "consumption_net": amount(total["net"] - total["transfer_net"]),
            "foreign_net": amount(total["foreign_net"]),
            "foreign_share": ratio(total["foreign_net"] / total["net"]) if total["net"] else 0.0,
            "avg_ticket": amount(round(total["spend"] / total["purchases"]))
            if total["purchases"] else 0.0,
        },
    }
    return out


def _group_key(grp: str, kind: str, group_by: str) -> dict:
    if group_by == "source":
        ref = rows.source_ref(kind, grp)
        return {"source": ref, "resource_ref": dict(ref)}
    return {"category": grp}


def _group_cell(c: dict, group_by: str) -> dict:
    return {**_group_key(c["grp"], c["kind"], group_by), "count": c["count"],
            **_money(c, "spend", "refunds", "net")}


def _group_total(g: dict, group_by: str, total_net: int) -> dict:
    return {**_group_key(g["grp"], g["kind"], group_by), "count": g["count"],
            **_money(g, "spend", "refunds", "net"),
            "share": ratio(g["net"] / total_net) if total_net else 0.0,
            "pending_count": g["pending_count"], "first_date": g["first"],
            "last_date": g["last"]}


def by_category(conn, view: View, filters: Filters) -> dict:
    params: list = []
    sql = (f"WITH v AS ({visible_tx(view, filters, params)})"
           f" SELECT CASE WHEN category = '' THEN '{UNCATEGORIZED}' ELSE category END AS name,"
           f" COUNT(*) AS count, {_SPEND} AS spend, {_REFUNDS} AS refunds, {_NET} AS net"
           " FROM v GROUP BY name")
    found = [dict(r) for r in conn.execute(sql, params)]
    total = sum(c["net"] for c in found)
    found.sort(key=lambda c: (-c["net"], c["name"]))
    return {"start": filters.start, "end": filters.end, "net": amount(total),
            "items": [{"category": c["name"], "count": c["count"],
                       **_money(c, "spend", "refunds", "net"),
                       "share": ratio(c["net"] / total) if total else 0.0} for c in found]}


# ---- merchants -----------------------------------------------------------------------

def merchants(conn, view: View, filters: Filters) -> list[dict]:
    """One row per merchant_key (rows with an empty description are not a
    merchant, as in analysis.js)."""
    params: list = []
    sql = (f"WITH v AS ({visible_tx(view, filters, params)})"
           " SELECT merchant_key, MIN(description) AS name, COUNT(*) AS count,"
           f" {_NET} AS net, {_REFUNDS} AS refunds,"
           " COUNT(DISTINCT strftime('%Y-%m', date)) AS months,"
           " group_concat(DISTINCT source_id) AS sources,"
           " SUM(CASE WHEN charged_x100 < 0 THEN 1 ELSE 0 END) AS purchases,"
           " SUM(CASE WHEN charged_x100 < 0 THEN -charged_x100 ELSE 0 END) AS purchase_sum,"
           # The squares feed only a ratio (the coefficient of variation), so
           # they are summed as REAL: an integer SUM of squares passes 2^63,
           # which SQLite refuses (a 503 for every key), with amounts far
           # below the ingest bound, e.g. two purchases of 22 million ILS.
           " SUM(CASE WHEN charged_x100 < 0"
           " THEN CAST(charged_x100 AS REAL) * charged_x100 ELSE 0 END)"
           " AS purchase_squares,"
           " MAX(date) AS last_date, MAX(is_transfer) AS transfer,"
           " MAX(CASE WHEN installment_total > 1 THEN 1 ELSE 0 END) AS installments"
           " FROM v WHERE merchant_key != '' GROUP BY merchant_key")
    out = []
    for r in conn.execute(sql, params):
        m = dict(r)
        m["sources"] = sorted((m["sources"] or "").split(",")) if m["sources"] else []
        m["cv"] = cv_from_sums(m["purchases"], m["purchase_sum"], m["purchase_squares"])
        out.append(m)
    return out


def _merchant(m: dict, names: bool) -> dict:
    return {"merchant": rows.merchant_name(m["name"], m["merchant_key"], names),
            "count": m["count"], "net": amount(m["net"]), "refunds": amount(m["refunds"]),
            "months": m["months"],
            "avg_per_month": amount(round(m["net"] / m["months"])) if m["months"] else 0.0,
            "avg_amount": amount(round(m["purchase_sum"] / m["purchases"]))
            if m["purchases"] else 0.0,
            "last_date": m["last_date"], "sources": m["sources"],
            "transfer": bool(m["transfer"])}


def _by_net(ms: list[dict]) -> list[dict]:
    return sorted(ms, key=lambda m: (-m["net"], m["merchant_key"]))


def top_merchants(conn, view: View, filters: Filters, limit: int, names: bool) -> dict:
    found = _by_net([m for m in merchants(conn, view, filters) if m["net"] > 0])
    return {"items": [_merchant(m, names) for m in found[:limit]]}


def recurring_merchants(conn, view: View, filters: Filters, limit: int, names: bool) -> dict:
    found = _by_net([m for m in merchants(conn, view, filters)
                     if m["months"] >= 3 and m["net"] > 0])
    return {"items": [_merchant(m, names) for m in found[:limit]]}


def subscriptions(conn, view: View, filters: Filters, limit: int, names: bool,
                  clock: Clock) -> dict:
    today = clock.today()
    # analysis.js: `${YYYY}-${max(1, MM - 1)}` (a January keeps the current month).
    active_from = f"{today.year}-{max(1, today.month - 1):02d}"
    found = [m for m in merchants(conn, view, filters)
             if m["months"] >= 4 and m["net"] > 0 and not m["transfer"]
             and not m["installments"] and m["purchases"] <= m["months"] + 1
             and m["cv"] <= 0.15]
    for m in found:
        m["monthly_x100"] = m["purchase_sum"] / m["purchases"] if m["purchases"] else 0
    found.sort(key=lambda m: (-m["monthly_x100"], m["merchant_key"]))
    return {"items": [{**_merchant(m, names),
                       "monthly_amount": amount(round(m["monthly_x100"])),
                       "cv": ratio(m["cv"]),
                       "active": m["last_date"] >= active_from} for m in found[:limit]]}


# ---- installments -----------------------------------------------------------------------

def installments(conn, view: View, filters: Filters, active_only: bool, names: bool) -> dict:
    params: list = []
    plan_rows = replace(filters, installments_only=True)
    sql = (f"WITH v AS ({visible_tx(view, plan_rows, params)})"
           " SELECT source_id, MAX(source_kind) AS kind, merchant_key,"
           " installment_total AS total, ABS(original_x100) AS deal_x100,"
           " MIN(description) AS name,"
           " MAX(CASE WHEN installment_number > 0 THEN installment_number END) AS max_number,"
           " SUM(ABS(charged_x100)) AS charged_sum, COUNT(*) AS charged_count,"
           f" {_NET} AS net, MIN(date) AS first_date, MAX(date) AS last_date"
           " FROM v GROUP BY source_id, merchant_key, installment_total, ABS(original_x100)")
    plans = []
    for r in conn.execute(sql, params):
        p = dict(r)
        p["paid"] = p["max_number"] if p["max_number"] is not None else p["charged_count"]
        p["per_x100"] = p["charged_sum"] / p["charged_count"] if p["charged_count"] else 0
        p["remaining"] = max(0, p["total"] - p["paid"])
        p["remaining_x100"] = p["per_x100"] * p["remaining"]
        plans.append(p)
    active = [p for p in plans if p["remaining"] > 0]
    shown = active if active_only else plans
    shown.sort(key=lambda p: (-p["remaining_x100"], p["source_id"], p["merchant_key"],
                              p["total"], p["deal_x100"]))
    return {
        "active_count": len(active),
        "completed_count": len([p for p in plans if p["remaining"] == 0]),
        "remaining_total": amount(round(sum(p["remaining_x100"] for p in active))),
        "monthly_commitment": amount(round(sum(p["per_x100"] for p in active))),
        "paid_this_period": amount(sum(p["net"] for p in plans)),
        "items": [{"source": rows.source_ref(p["kind"], p["source_id"]),
                   "description": rows.merchant_name(p["name"], p["merchant_key"], names),
                   "total": p["total"], "deal_amount": amount(p["deal_x100"]),
                   "paid": p["paid"], "remaining": p["remaining"],
                   "per_installment": amount(round(p["per_x100"])),
                   "remaining_amount": amount(round(p["remaining_x100"])),
                   "first_date": p["first_date"], "last_date": p["last_date"],
                   "resource_ref": rows.source_ref(p["kind"], p["source_id"])}
                  for p in shown],
    }


# ---- foreign currency ----------------------------------------------------------------------

def foreign_currency(conn, view: View, filters: Filters) -> dict:
    params: list = []
    only = replace(filters, foreign_only=True)
    sql = (f"WITH v AS ({visible_tx(view, only, params)})"
           " SELECT upper(original_currency) AS currency, COUNT(*) AS count,"
           " SUM(-original_x100) AS original_total, SUM(-charged_x100) AS charged_total,"
           " group_concat(DISTINCT strftime('%Y-%m', date)) AS months"
           " FROM v GROUP BY upper(original_currency)")
    found = [dict(r) for r in conn.execute(sql, params)]
    found.sort(key=lambda f: (-f["charged_total"], f["currency"]))
    return {
        "foreign_net": amount(sum(f["charged_total"] for f in found)),
        "items": [{"currency": f["currency"], "count": f["count"],
                   "original_total": amount(f["original_total"]),
                   "charged_total": amount(f["charged_total"]),
                   "implied_rate": ratio(f["charged_total"] / f["original_total"])
                   if f["original_total"] else None,
                   "months": sorted((f["months"] or "").split(",")) if f["months"] else []}
                  for f in found],
    }
