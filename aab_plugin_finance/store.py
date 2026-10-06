"""The finance database: connections, error mapping, and the visibility SQL.

One SQLite file in the plugin's own volume (FINANCE_DB), opened per request
(busy_timeout=5000, journal_mode=WAL, foreign_keys=ON). The container runs
one uvicorn worker, so there is one writer process; requests are threads of
it, and every write is one `BEGIN IMMEDIATE` transaction.

Visibility is applied INSIDE every query, never by filtering fetched rows:
post-filtering would corrupt LIMIT and keyset paging and, for aggregates,
let a hidden card move a total. `tx_clause` is the one fragment every read
uses. It admits a transaction only when
  * its card or account is admitted by that kind's visibility (deny, and
    allow_only when the capability lists cards or accounts; `[]` = none),
  * the transaction itself is not denied (owner-hidden transactions),
  * its company is not denied,
  * and, under `date_window_days`, it is dated on or after the cutoff.
Column names in the fragments are trusted literals from this package; every
value is a bound parameter (the pattern of the gateway's WhatsApp archive,
`_visibility_clause`).

Errors: any `sqlite3.Error` becomes AdapterError(503) ("not performed, safe
to retry"). A write that fails anywhere before COMMIT is rolled back and is
also a 503, whatever the exception, because nothing was performed.
"""

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from aab_plugin_runtime import AdapterError
from aab_plugin_runtime.logging_setup import kv

from . import schema
from .ids import label_for
from .scope import View, Visibility

log = logging.getLogger(__name__)

UNAVAILABLE = "finance store temporarily unavailable"


class Store:
    """The database at `path` (FINANCE_DB)."""

    def __init__(self, path: str):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("FINANCE_DB must be a non-empty path")
        # A URI-looking path could smuggle connection parameters; refuse at boot.
        if path.startswith("file:") or "?" in path or "#" in path:
            raise ValueError("FINANCE_DB must be a plain file path")
        self.path = path
        self._ready = False
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"Store(path={self.path!r})"

    # ---- connections -----------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        # isolation_level=None: no implicit transactions; writes say BEGIN
        # IMMEDIATE themselves, so the transaction boundary is explicit.
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            if not self._ready:
                with self._lock:
                    if not self._ready:
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            schema.apply(conn)
                            conn.execute("COMMIT")
                        except BaseException:
                            if conn.in_transaction:
                                conn.execute("ROLLBACK")
                            raise
                        self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    def ensure(self) -> None:
        """Create the schema now (boot). Raises sqlite3.Error / OSError."""
        self._open().close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = self._open()
        except sqlite3.Error as exc:
            log.warning("finance store unavailable %s", kv(error=type(exc).__name__))
            raise AdapterError(503, UNAVAILABLE) from exc
        try:
            yield conn
        except sqlite3.Error as exc:
            log.warning("finance store error %s", kv(error=type(exc).__name__))
            raise AdapterError(503, UNAVAILABLE) from exc
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One BEGIN IMMEDIATE transaction. An AdapterError raised inside
        (a 400, 404 or 409 decided mid-transaction) rolls back and passes
        through; anything else rolls back and is a 503."""
        try:
            conn = self._open()
        except sqlite3.Error as exc:
            log.warning("finance store unavailable %s", kv(error=type(exc).__name__))
            raise AdapterError(503, UNAVAILABLE) from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except AdapterError:
            _rollback(conn)
            raise
        except Exception as exc:
            _rollback(conn)
            # The exception type only: a message could quote row content.
            log.warning("finance write rolled back %s", kv(error=type(exc).__name__))
            raise AdapterError(503, UNAVAILABLE) from exc
        finally:
            conn.close()

    def ping(self) -> bool:
        with self.read() as conn:
            conn.execute("SELECT 1").fetchone()
        return True

    # ---- owner-plane lookups (no visibility: /label and /resolve) -------------

    def names(self, kind: str, ids: list[str]) -> dict[str, str]:
        """Display names for /label (approval cards, the console). Never
        amounts or merchants: a transaction is named by date and card."""
        wanted = [i for i in dict.fromkeys(ids) if isinstance(i, str)][:500]
        if not wanted:
            return {}
        marks = ",".join("?" for _ in wanted)
        with self.read() as conn:
            if kind in ("card", "account"):
                rows = conn.execute(f"SELECT id, company, label FROM sources"
                                    f" WHERE kind = ? AND id IN ({marks})", [kind, *wanted])
                return {r["id"]: label_for(r["id"], r["company"], r["label"]) for r in rows}
            if kind == "transaction":
                rows = conn.execute(
                    "SELECT t.id, t.date, s.id AS sid, s.company, s.label FROM transactions t"
                    f" JOIN sources s ON s.id = t.source_id WHERE t.id IN ({marks})", wanted)
                return {r["id"]: f"{r['date']} {label_for(r['sid'], r['company'], r['label'])}"
                        for r in rows}
        return {}

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        """Cards or accounts whose id or label contains `query`."""
        like = "%" + escape_like(query.strip()) + "%"
        with self.read() as conn:
            rows = conn.execute(
                "SELECT id, company, label FROM sources WHERE kind = ?"
                " AND (id LIKE ? ESCAPE '\\' OR label LIKE ? ESCAPE '\\' OR last4 LIKE ? ESCAPE '\\')"
                " ORDER BY company, id LIMIT ?", (kind, like, like, like, limit)).fetchall()
        return [{"id": r["id"], "label": label_for(r["id"], r["company"], r["label"]),
                 "kind": kind} for r in rows]


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass                      # the connection is closed next anyway


def escape_like(text: str) -> str:
    """Make `text` literal inside a LIKE pattern (ESCAPE '\\')."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---- visibility fragments ----------------------------------------------------------

def visibility_clause(column: str, vis: Visibility, params: list) -> str:
    """' AND ...' for one kind on `column`: deny wins, `allow` None is
    unrestricted, an empty allow set admits nothing (AND 0)."""
    clause = ""
    if vis.deny:
        ids = sorted(vis.deny)
        clause += f" AND {column} NOT IN ({','.join('?' for _ in ids)})"
        params.extend(ids)
    if vis.allow is not None:
        if not vis.allow:
            clause += " AND 0"
        else:
            ids = sorted(vis.allow)
            clause += f" AND {column} IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
    return clause


def source_clause(view: View, params: list, kind_col: str, id_col: str,
                  company_col: str) -> str:
    """A card answers to the card visibility, an account to the account
    one; a row of any other kind matches neither arm and is invisible."""
    sql = f" AND (({kind_col} = 'card'"
    sql += visibility_clause(id_col, view.card, params)
    sql += f") OR ({kind_col} = 'account'"
    sql += visibility_clause(id_col, view.account, params)
    sql += "))"
    sql += visibility_clause(company_col, view.company, params)
    return sql


def tx_clause(view: View, params: list, alias: str = "t") -> str:
    sql = source_clause(view, params, f"{alias}.source_kind", f"{alias}.source_id",
                        f"{alias}.company")
    sql += visibility_clause(f"{alias}.id", view.transaction, params)
    if view.since:
        sql += f" AND {alias}.date >= ?"
        params.append(view.since)
    return sql


@dataclass(frozen=True)
class Filters:
    """The caller's own filters, applied on top of visibility."""
    source: str | None = None
    start: str | None = None
    end: str | None = None
    status: str | None = None         # completed | pending
    kind: str | None = None           # purchases | refunds | transfers
    min_x100: int | None = None
    installments_only: bool = False
    foreign_only: bool = False
    purchases_only: bool = False


def filters_clause(f: Filters, params: list, alias: str = "t") -> str:
    sql = ""
    if f.source:
        sql += f" AND {alias}.source_id = ?"
        params.append(f.source)
    if f.start:
        sql += f" AND {alias}.date >= ?"
        params.append(f.start)
    if f.end:
        sql += f" AND {alias}.date <= ?"
        params.append(f.end)
    if f.status:
        sql += f" AND {alias}.status = ?"
        params.append(f.status)
    if f.kind == "purchases" or f.purchases_only:
        sql += f" AND {alias}.charged_x100 < 0"
    elif f.kind == "refunds":
        sql += f" AND {alias}.charged_x100 > 0"
    elif f.kind == "transfers":
        sql += f" AND {alias}.is_transfer = 1"
    if f.min_x100 is not None:
        sql += f" AND ABS({alias}.charged_x100) >= ?"
        params.append(f.min_x100)
    if f.installments_only:
        sql += f" AND {alias}.installment_total > 1"
    if f.foreign_only:
        sql += f" AND {alias}.is_foreign = 1"
    return sql


def visible_tx(view: View, filters: Filters, params: list) -> str:
    """SELECT of every visible transaction matching `filters`: the subquery
    each aggregate groups over, so they all see exactly the same rows."""
    return ("SELECT t.* FROM transactions t WHERE 1=1"
            + tx_clause(view, params) + filters_clause(filters, params))


# ---- line items --------------------------------------------------------------------

def _select(notes: bool) -> str:
    sql = "SELECT t.*" + (", n.note AS note" if notes else "") + " FROM transactions t"
    if notes:
        sql += " LEFT JOIN notes n ON n.key = t.key"
    return sql


def transactions_page(conn: sqlite3.Connection, view: View, filters: Filters, *,
                      notes: bool, limit: int,
                      cursor: tuple[str, str] | None = None) -> list[sqlite3.Row]:
    """Newest first on the (date, id) keyset; `limit` rows at most."""
    params: list = []
    sql = _select(notes) + " WHERE 1=1" + tx_clause(view, params) + filters_clause(filters, params)
    if cursor is not None:
        sql += " AND (t.date < ? OR (t.date = ? AND t.id < ?))"
        params.extend([cursor[0], cursor[0], cursor[1]])
    sql += " ORDER BY t.date DESC, t.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def search_page(conn: sqlite3.Connection, view: View, filters: Filters, query: str, *,
                notes: bool, names: bool, limit: int) -> list[sqlite3.Row]:
    """LIKE search. Fields an agent may not read are not searched either: a
    match would tell it what a redacted description says."""
    params: list = []
    sql = _select(notes) + " WHERE 1=1" + tx_clause(view, params) + filters_clause(filters, params)
    like = "%" + escape_like(query) + "%"
    arms = ["t.category LIKE ? ESCAPE '\\'"]
    if names:
        arms += ["t.description LIKE ? ESCAPE '\\'", "t.memo LIKE ? ESCAPE '\\'"]
    if notes:
        arms.append("n.note LIKE ? ESCAPE '\\'")
    sql += " AND (" + " OR ".join(arms) + ")"
    params.extend([like] * len(arms))
    sql += " ORDER BY t.date DESC, t.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_visible(conn: sqlite3.Connection, view: View, tx: str, *,
                notes: bool) -> sqlite3.Row | None:
    params: list = [tx]
    sql = _select(notes) + " WHERE t.id = ?" + tx_clause(view, params)
    return conn.execute(sql, params).fetchone()


def largest(conn: sqlite3.Connection, view: View, filters: Filters, *,
            limit: int) -> list[sqlite3.Row]:
    params: list = []
    sql = (_select(False) + " WHERE t.charged_x100 < 0" + tx_clause(view, params)
           + filters_clause(filters, params)
           + " ORDER BY t.charged_x100 ASC, t.date DESC, t.id ASC LIMIT ?")
    params.append(limit)
    return conn.execute(sql, params).fetchall()


# ---- sources -------------------------------------------------------------------------

def get_source(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def visible_sources(conn: sqlite3.Connection, view: View,
                    kind: str | None = None) -> list[sqlite3.Row]:
    """Each visible source with its visible transactions' count and span.
    Hidden transactions are left out of the counts (the JOIN condition)."""
    params: list = []
    join = visibility_clause("t.id", view.transaction, params)
    sql = ("SELECT s.*, COUNT(t.id) AS tx_count, MIN(t.date) AS first_date,"
           " MAX(t.date) AS last_date,"
           " (SELECT MAX(r.applied_at) FROM ingest_runs r WHERE r.source_id = s.id"
           "  AND r.status = 'applied') AS last_run_at,"
           " (SELECT r.scraped_at FROM ingest_runs r WHERE r.source_id = s.id"
           "  AND r.status = 'applied' ORDER BY r.applied_at DESC LIMIT 1) AS last_scraped_at"
           " FROM sources s LEFT JOIN transactions t ON t.source_id = s.id" + join
           + " WHERE 1=1" + source_clause(view, params, "s.kind", "s.id", "s.company"))
    if kind:
        sql += " AND s.kind = ?"
        params.append(kind)
    sql += " GROUP BY s.id ORDER BY s.company, s.id"
    return conn.execute(sql, params).fetchall()
