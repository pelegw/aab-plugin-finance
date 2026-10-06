"""Agent notes on transactions: `set_note` and `list_notes`.

A note is keyed by the transaction's row key, so it survives every later
upload of the same row (the merge never touches the notes table) and goes
away only with the row itself (a stale pending row, ON DELETE CASCADE),
exactly as the workbook's Notes column behaved. Every write is appended to
`note_history` with the broker's request id, so the decision record and the
note can be matched.

Visibility: a note can be written only on a transaction the caller could
read (its card or account visible, the transaction not hidden, inside the
date window); anything else is the one 404 a missing transaction gets.
`notes: false` and `detail: aggregate` make both actions a 403 before the
database is consulted (the adapter checks them).

Note text is agent input that the owner and other agents will read; it is
stored as given and never logged.
"""

import logging
import re

from aab_plugin_runtime import AdapterError
from aab_plugin_runtime.logging_setup import kv

from . import rows, store as st
from .clock import Clock
from .scope import View

log = logging.getLogger(__name__)

NOT_FOUND = "not found"
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def request_id(value: str) -> str | None:
    """The broker's request id when it has the documented shape, else none."""
    return value if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) else None


def set_note(store: st.Store, view: View, tx: str, note: str, rid: str,
             clock: Clock) -> dict:
    now = clock.iso_now()
    with store.write() as conn:
        row = st.get_visible(conn, view, tx, notes=False)
        if row is None:
            raise AdapterError(404, NOT_FOUND)
        if note:
            conn.execute("INSERT INTO notes (key, note, author, updated_at, request_id)"
                         " VALUES (?, ?, 'agent', ?, ?) ON CONFLICT(key) DO UPDATE SET"
                         " note = excluded.note, author = excluded.author,"
                         " updated_at = excluded.updated_at, request_id = excluded.request_id",
                         (row["key"], note, now, request_id(rid)))
        else:
            conn.execute("DELETE FROM notes WHERE key = ?", (row["key"],))
        conn.execute("INSERT INTO note_history (key, note, author, created_at, request_id)"
                     " VALUES (?, ?, 'agent', ?, ?)", (row["key"], note, now, request_id(rid)))
    log.info("note written %s", kv(cleared=not note))
    # No resource_ref: the write happened, and a broker post-filter 404 on
    # its result would tell the agent it had not.
    return {"id": tx, "note": note or None, "updated_at": now}


def list_notes(conn, view: View, filters: st.Filters, *, names: bool,
               limit: int) -> list[dict]:
    params: list = []
    sql = ("SELECT t.*, n.note AS note, n.updated_at AS note_updated_at FROM notes n"
           " JOIN transactions t ON t.key = n.key WHERE 1=1"
           + st.tx_clause(view, params) + st.filters_clause(filters, params)
           + " ORDER BY n.updated_at DESC, t.id DESC LIMIT ?")
    params.append(limit)
    found = conn.execute(sql, params).fetchall()
    out = []
    for r in found:
        item = {"note": r["note"], "updated_at": r["note_updated_at"],
                "transaction": rows.transaction(r, notes=False, names=names)}
        item["resource_ref"] = item["transaction"]["resource_ref"]
        out.append(item)
    return out
