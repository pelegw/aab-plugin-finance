"""The finance plugin adapter: every manifest action, inside the broker's scope.

`aab_plugin_runtime` serves this adapter in the plugin-finance container. The
broker reaches it over the plugin API. The shape comes from the gateway's
WhatsApp adapter:
  * A dispatch table.
  * A parity check between the manifest and the handlers at boot. If the
    manifest declares an action that this code does not implement, or the
    reverse, the plugin does not start.
  * Store failures map to 503.

This module makes sure of these points, whatever the broker already checked:
  * CallScope parsing fails closed (scope.py). A malformed scope, or a
    constraint that this manifest does not declare, is a 400.
    `allow_only: []` sees nothing.
  * Visibility lives INSIDE every query (store.tx_clause), aggregates
    included. It covers cards and accounts, owner-hidden transactions,
    denied companies and the date window.
  * Hidden == missing. A hidden or out-of-window transaction, card or
    account gets the same 404 as one that does not exist.
  * `detail: aggregate` permits only snapshot_info, list_sources,
    monthly_summary, by_category, foreign_currency, list_runs and
    list_refresh_requests. Every other action that it applies to is a 403.
  * `notes: false` strips notes and makes set_note and list_notes a 403.
  * `merchant_names: false` replaces descriptions with opaque merchant ids.
    It also stops searches from matching descriptions.
  * Every row that names a card or an account carries `resource_ref`. The
    broker's post-filter can then drop anything that this code let through.
  * `ingest_snapshot` answers only to the company's visibility, so hidden
    cards do not block uploads. Reads of a hidden card stay 404.

`resolve` and `label` apply no visibility. They serve the owner's console and
approval cards, and the broker itself filters `resolve` for agents.
Transactions have no resolve at all (see the manifest).
"""

from collections.abc import Callable
from pathlib import Path

import yaml
from aab_plugin_runtime import AdapterError, Result

from . import aggregates, annotations, ingest, refresh, rows
from . import params as p
from . import store as st
from .clock import Clock
from .ids import (company_label, label_for, normalize_company, normalize_source_id,
                  normalize_tx_id)
from .scope import CallScope, View, constraint_forms

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")
NOT_FOUND = "not found"                  # one message for missing AND hidden
LINE_ITEMS_DENIED = "line items are outside your grant"
NOTES_DENIED = "notes are outside your grant"
RESOLVE_LIMIT = 50
_CURSOR_SEP = "|"


class NoneConnection:
    """Connection kind `none`. The plugin holds no credential at all, so it
    has nothing to pair, mint or disconnect. The echo test plugin uses the
    same pattern."""

    def start(self, enabled_plugins: list[str]) -> dict:
        return {"kind": "none"}

    def finish(self, code, state, installation_id) -> dict:
        return {"ok": True}

    def qr_png(self) -> bytes:
        raise AdapterError(404, "this plugin has no QR code")

    def disconnect(self) -> dict:
        raise AdapterError(409, "nothing to disconnect: this plugin holds no credential")

    def status(self) -> dict:
        return {"kind": "none", "connected": True}

    def mint(self, requirements: dict):
        return None


class FinanceAdapter:
    """The `aab_plugin_runtime` PluginAdapter for plugin id `finance`."""

    def __init__(self, store: st.Store, clock: Clock | None = None, *,
                 manifest_path: Path = MANIFEST_PATH):
        self.manifest = yaml.safe_load(Path(manifest_path).read_text(encoding="utf-8"))
        self.store = store
        self.clock = clock or Clock()
        self.connection = NoneConnection()
        self._forms = constraint_forms(self.manifest)
        self._actions: dict[str, Callable[[dict, CallScope], dict]] = {
            "snapshot_info": self._snapshot_info,
            "list_sources": self._list_sources,
            "list_transactions": self._list_transactions,
            "get_transaction": self._get_transaction,
            "search_transactions": self._search_transactions,
            "monthly_summary": self._monthly_summary,
            "by_category": self._by_category,
            "top_merchants": self._top_merchants,
            "recurring_merchants": self._recurring_merchants,
            "subscriptions": self._subscriptions,
            "installments": self._installments,
            "foreign_currency": self._foreign_currency,
            "largest_purchases": self._largest_purchases,
            "list_notes": self._list_notes,
            "list_runs": self._list_runs,
            "list_refresh_requests": self._list_refresh_requests,
            "set_note": self._set_note,
            "request_refresh": self._request_refresh,
            "ingest_snapshot": self._ingest_snapshot,
            "report_refresh": self._report_refresh,
        }
        # The manifest is the contract that the broker enforces. If it
        # declares an action that this code does not implement, or the
        # reverse, that is a packaging bug. The container then does not start.
        declared = {a["name"] for a in self.manifest.get("actions", [])}
        if declared != set(self._actions):
            raise RuntimeError(f"manifest/adapter action mismatch: "
                               f"{sorted(declared ^ set(self._actions))}")

    # ---- lifecycle -------------------------------------------------------------

    def configure(self, config: dict, secrets) -> None:
        """Nothing is configurable over the network, on purpose. The database
        path comes from this container's env (FINANCE_DB). No call to
        /configure can point the plugin at another file."""

    def status(self) -> dict:
        """503 (from the store) when the store cannot open the database."""
        with self.store.read() as conn:
            sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            last = conn.execute("SELECT value FROM meta WHERE key = 'last_ingest_at'").fetchone()
        return {"connected": True, "healthy": True, "health": "ok", "enforcement": "proxy",
                "sources": sources, "transactions": count,
                "last_ingest_at": last[0] if last else None}

    # ---- resources ---------------------------------------------------------------

    def normalize(self, kind: str, value: str) -> str:
        if kind in ("card", "account"):
            return normalize_source_id(value)
        if kind == "company":
            return normalize_company(value)
        if kind == "transaction":
            return normalize_tx_id(value)
        raise AdapterError(400, f"unknown resource kind {kind!r}")

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        if kind not in ("card", "account", "company", "transaction"):
            raise AdapterError(400, f"unknown resource kind {kind!r}")
        if kind not in ("card", "account") or not (query or "").strip():
            return []                     # companies and transactions have no lookup by name
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise AdapterError(400, "limit must be an integer")
        return self.store.resolve(kind, query, max(1, min(limit, RESOLVE_LIMIT)))

    def label(self, kind: str, ids: list[str]) -> dict[str, str]:
        if kind == "company":
            return {i: company_label(i) for i in ids if isinstance(i, str)}
        if kind not in ("card", "account", "transaction"):
            raise AdapterError(400, f"unknown resource kind {kind!r}")
        return self.store.names(kind, list(ids))

    # ---- dispatch ------------------------------------------------------------------

    def perform(self, action: str, params: dict, scope: dict) -> Result:
        handler = self._actions.get(action)
        if handler is None:
            raise AdapterError(404, f"unknown action {action!r}")
        if not isinstance(params, dict):
            raise AdapterError(400, "params must be an object")
        return Result(data=handler(params, CallScope(scope, self._forms)))

    def _view(self, call: CallScope, *, window: bool = True) -> View:
        """What the SQL can see for this call. It is one method, so a test can
        make the adapter 'leak' and prove that the broker's post-filter still
        holds."""
        days = call.bound("date_window_days") if window else None
        return View(card=call.vis("card"), account=call.vis("account"),
                    transaction=call.vis("transaction"), company=call.vis("company"),
                    since=self.clock.days_ago(days) if days is not None else None)

    @staticmethod
    def _line_items(call: CallScope) -> None:
        if call.level("detail", "line_items") == "aggregate":
            raise AdapterError(403, LINE_ITEMS_DENIED)

    def _filters(self, conn, params: dict, view: View, *, allow_month: bool = False,
                 **extra) -> st.Filters:
        """The caller's `source` and date filters. The `source` gets a 404
        unless it exists and is visible: a hidden card is a missing card."""
        start, end, _ = p.period(params, allow_month=allow_month)
        return st.Filters(source=self._visible_source(conn, params, view), start=start,
                          end=end, **extra)

    @staticmethod
    def _visible_source(conn, params: dict, view: View) -> str | None:
        raw = p.text(params, "source", max_len=64)
        if raw is None:
            return None
        source_id = normalize_source_id(raw)
        row = st.get_source(conn, source_id)
        if row is None or not view.admits_source(row["kind"], row["id"], row["company"]):
            raise AdapterError(404, NOT_FOUND)
        return source_id

    # ---- reads ---------------------------------------------------------------------

    def _snapshot_info(self, params: dict, call: CallScope) -> dict:
        view = self._view(call, window=False)
        with self.store.write() as conn:      # the refresh expiry is a lazy write
            refresh.expire(conn, self.clock)
            found = st.visible_sources(conn, view)
            pending = refresh.pending_count(conn)
        sources, companies = [], {}
        for r in found:
            label = label_for(r["id"], r["company"], r["label"])
            sources.append({"source": rows.source_ref(r["kind"], r["id"]), "label": label,
                            "company": r["company"], "count": r["tx_count"],
                            "first_date": r["first_date"], "last_date": r["last_date"],
                            "last_run_at": r["last_run_at"],
                            "last_scraped_at": r["last_scraped_at"],
                            "resource_ref": rows.source_ref(r["kind"], r["id"])})
            c = companies.setdefault(r["company"], {"company": r["company"], "sources": 0,
                                                    "count": 0, "last_ingest_at": None})
            c["sources"] += 1
            c["count"] += r["tx_count"]
            if r["last_run_at"] and (c["last_ingest_at"] is None
                                     or r["last_run_at"] > c["last_ingest_at"]):
                c["last_ingest_at"] = r["last_run_at"]
        firsts = [s["first_date"] for s in sources if s["first_date"]]
        lasts = [s["last_date"] for s in sources if s["last_date"]]
        runs = [s["last_run_at"] for s in sources if s["last_run_at"]]
        return {"today": self.clock.today().isoformat(), "as_of": self.clock.iso_now(),
                "last_ingest_at": max(runs) if runs else None,
                "transactions": sum(s["count"] for s in sources),
                "first_date": min(firsts) if firsts else None,
                "last_date": max(lasts) if lasts else None,
                "companies": sorted(companies.values(), key=lambda c: c["company"]),
                "sources": sources, "pending_refreshes": pending}

    def _list_sources(self, params: dict, call: CallScope) -> dict:
        kind = p.choice(params, "kind", ("card", "account", "all"), default="all")
        with self.store.read() as conn:
            found = st.visible_sources(conn, self._view(call, window=False),
                                       None if kind == "all" else kind)
        return {"items": [rows.source(r, label_for(r["id"], r["company"], r["label"]))
                          for r in found]}

    def _list_transactions(self, params: dict, call: CallScope) -> dict:
        self._line_items(call)
        notes, names = call.flag("notes"), call.flag("merchant_names")
        status = p.choice(params, "status", ("all", "completed", "pending"), default="all")
        kind = p.choice(params, "kind", ("all", "purchases", "refunds", "transfers"),
                        default="all")
        min_x100 = p.integer(params, "min_x100", minimum=0, maximum=ingest.MAX_ABS_X100)
        limit = p.limit(params, default=100, maximum=500)
        cursor = _cursor(p.text(params, "cursor", max_len=64))
        view = self._view(call)
        with self.store.read() as conn:
            filters = self._filters(conn, params, view, allow_month=True,
                                    status=None if status == "all" else status,
                                    kind=None if kind == "all" else kind, min_x100=min_x100)
            found = st.transactions_page(conn, view, filters, notes=notes, limit=limit + 1,
                                         cursor=cursor)
        more = len(found) > limit
        found = found[:limit]
        return {"items": [rows.transaction(r, notes=notes, names=names) for r in found],
                "next_cursor": f"{found[-1]['date']}{_CURSOR_SEP}{found[-1]['id']}"
                if more else None}

    def _get_transaction(self, params: dict, call: CallScope) -> dict:
        self._line_items(call)
        tx = normalize_tx_id(p.text(params, "id", required=True, max_len=32))
        notes = call.flag("notes")
        with self.store.read() as conn:
            row = st.get_visible(conn, self._view(call), tx, notes=notes)
        if row is None:
            raise AdapterError(404, NOT_FOUND)
        return rows.transaction(row, notes=notes, names=call.flag("merchant_names"))

    def _search_transactions(self, params: dict, call: CallScope) -> dict:
        self._line_items(call)
        query = p.text(params, "query", required=True, min_len=2, max_len=200)
        if len(query.strip()) < 2:
            raise AdapterError(400, "query must be at least 2 characters")
        notes, names = call.flag("notes"), call.flag("merchant_names")
        limit = p.limit(params, default=50, maximum=200)
        view = self._view(call)
        with self.store.read() as conn:
            filters = self._filters(conn, params, view)
            found = st.search_page(conn, view, filters, query.strip(), notes=notes,
                                   names=names, limit=limit)
        return {"items": [rows.transaction(r, notes=notes, names=names) for r in found]}

    def _monthly_summary(self, params: dict, call: CallScope) -> dict:
        group_by = p.choice(params, "group_by", ("source", "category", "none"),
                            default="source")
        start, end, year = p.period(params, allow_year=True)
        view = self._view(call)
        with self.store.read() as conn:
            filters = st.Filters(start=start, end=end)
            return aggregates.monthly_summary(conn, view, filters, group_by, year, self.clock)

    def _by_category(self, params: dict, call: CallScope) -> dict:
        view = self._view(call)
        with self.store.read() as conn:
            return aggregates.by_category(conn, view, self._filters(conn, params, view))

    def _merchant_action(self, params: dict, call: CallScope, fn, **kw) -> dict:
        self._line_items(call)
        limit = p.limit(params, default=25, maximum=100)
        view = self._view(call)
        with self.store.read() as conn:
            return fn(conn, view, self._filters(conn, params, view), limit,
                      call.flag("merchant_names"), **kw)

    def _top_merchants(self, params: dict, call: CallScope) -> dict:
        return self._merchant_action(params, call, aggregates.top_merchants)

    def _recurring_merchants(self, params: dict, call: CallScope) -> dict:
        return self._merchant_action(params, call, aggregates.recurring_merchants)

    def _subscriptions(self, params: dict, call: CallScope) -> dict:
        return self._merchant_action(params, call, aggregates.subscriptions, clock=self.clock)

    def _installments(self, params: dict, call: CallScope) -> dict:
        self._line_items(call)
        active_only = p.boolean(params, "active_only", default=True)
        view = self._view(call)
        with self.store.read() as conn:
            return aggregates.installments(conn, view, self._filters(conn, params, view),
                                           active_only, call.flag("merchant_names"))

    def _foreign_currency(self, params: dict, call: CallScope) -> dict:
        view = self._view(call)
        with self.store.read() as conn:
            return aggregates.foreign_currency(conn, view, self._filters(conn, params, view))

    def _largest_purchases(self, params: dict, call: CallScope) -> dict:
        self._line_items(call)
        limit = p.limit(params, default=15, maximum=100)
        names = call.flag("merchant_names")
        view = self._view(call)
        with self.store.read() as conn:
            found = st.largest(conn, view, self._filters(conn, params, view), limit=limit)
        return {"items": [rows.transaction(r, notes=False, names=names) for r in found]}

    def _list_notes(self, params: dict, call: CallScope) -> dict:
        self._line_items(call)
        if not call.flag("notes"):
            raise AdapterError(403, NOTES_DENIED)
        limit = p.limit(params, default=100, maximum=500)
        view = self._view(call)
        with self.store.read() as conn:
            filters = st.Filters(source=self._visible_source(conn, params, view))
            return {"items": annotations.list_notes(conn, view, filters,
                                                    names=call.flag("merchant_names"),
                                                    limit=limit)}

    def _list_runs(self, params: dict, call: CallScope) -> dict:
        limit = p.limit(params, default=50, maximum=500)
        view = self._view(call, window=False)
        with self.store.write() as conn:      # the abandon sweep is a lazy write
            abandoned = ingest.sweep_abandoned(conn, self.clock)
            source = self._visible_source(conn, params, view)
            args: list = []
            sql = ("SELECT r.*, (SELECT COUNT(*) FROM ingest_chunks c WHERE c.run_id = r.run_id)"
                   " AS chunks_received FROM ingest_runs r WHERE 1=1"
                   + st.source_clause(view, args, "r.source_kind", "r.source_id", "r.company"))
            if source:
                sql += " AND r.source_id = ?"
                args.append(source)
            sql += " ORDER BY r.created_at DESC, r.run_id DESC LIMIT ?"
            args.append(limit)
            found = conn.execute(sql, args).fetchall()
        ingest.log_abandoned(abandoned)
        return {"items": [rows.run(r) for r in found]}

    def _list_refresh_requests(self, params: dict, call: CallScope) -> dict:
        status = p.choice(params, "status", (*refresh.STATUSES, "all"), default="approved")
        refresh_id = p.text(params, "refresh_id", max_len=64) or None
        limit = p.limit(params, default=50, maximum=200)
        return {"items": refresh.list_requests(self.store, status, refresh_id, limit,
                                               self.clock, call.vis("company"))}

    # ---- writes ----------------------------------------------------------------------

    def _set_note(self, params: dict, call: CallScope) -> dict:
        self._line_items(call)
        if not call.flag("notes"):
            raise AdapterError(403, NOTES_DENIED)
        tx = normalize_tx_id(p.text(params, "id", required=True, max_len=32))
        note = p.text(params, "note", required=True, max_len=2000)
        return annotations.set_note(self.store, self._view(call), tx, note, call.request_id,
                                    self.clock)

    def _request_refresh(self, params: dict, call: CallScope) -> dict:
        # modes: [draft]. The broker delivers this call only after the owner
        # approves it, so its arrival here IS the approval.
        return refresh.create_approved(self.store, params, call.request_id, self.clock)

    def _ingest_snapshot(self, params: dict, call: CallScope) -> dict:
        chunk = ingest.parse(params)
        return ingest.stage(self.store, chunk, self.clock, call.vis("company"))

    def _report_refresh(self, params: dict, call: CallScope) -> dict:
        return refresh.report(self.store, params, self.clock)


def _cursor(raw: str | None) -> tuple[str, str] | None:
    """A keyset cursor "YYYY-MM-DD|tx_<16 hex>" from a previous page."""
    if not raw:
        return None
    day, _, tx = raw.partition(_CURSOR_SEP)
    if not p.is_date(day):
        raise AdapterError(400, "cursor is not a cursor from this action")
    try:
        return day, normalize_tx_id(tx)
    except AdapterError:
        raise AdapterError(400, "cursor is not a cursor from this action") from None
