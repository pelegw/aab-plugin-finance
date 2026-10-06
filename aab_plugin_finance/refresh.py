"""Refresh requests: an agent asks, the owner approves, the scraper runs it.

`request_refresh` is `modes: [draft]` in the manifest, so the broker turns
every call into an approval card and only delivers it here after the owner
approved it; this module then records it as `approved`. The scraper polls
`list_refresh_requests`, claims one with `report_refresh running` and ends
it with `completed` or `failed`.

  approved --claim--> running --> completed | failed
  approved --7 days--> expired          (lazily, on the next list or report)

The claim is one `UPDATE ... WHERE status = 'approved'`: two pollers racing
for the same request cannot both win; the loser gets a 409. Finishing
requires `running`. An unknown id is a 404.

Logs carry refresh ids and statuses only; never the reason (agent text) or
the scraper's message.
"""

import json
import logging
import re
import uuid

from aab_plugin_runtime import AdapterError
from aab_plugin_runtime.logging_setup import kv

from . import params as p
from . import rows
from .annotations import request_id
from .clock import Clock
from .ingest import RUN_ID_RE
from .scope import Visibility
from .store import visibility_clause

log = logging.getLogger(__name__)

EXPIRY_SECONDS = 7 * 24 * 3600
COMPANIES = ("all", "cal", "max", "isracard", "amex")
STATUSES = ("approved", "running", "completed", "failed", "expired")
MAX_RUN_IDS = 200
REFRESH_ID_RE = re.compile(r"rr_[0-9a-f]{16}")


def create_approved(store, params: dict, rid: str, clock: Clock) -> dict:
    company = p.choice(params, "company", COMPANIES, default="all")
    start, end = p.day(params, "start", required=True), p.day(params, "end", required=True)
    if start > end:
        raise AdapterError(400, "start must not be after end")
    reason = p.text(params, "reason", required=True, min_len=1, max_len=300)
    if not reason.strip():
        raise AdapterError(400, "reason must not be empty")
    refresh_id = "rr_" + uuid.uuid4().hex[:16]
    created, expires = clock.iso_now(), clock.iso_now(EXPIRY_SECONDS)
    with store.write() as conn:
        conn.execute("INSERT INTO refresh_requests (id, company, range_start, range_end, reason,"
                     " status, created_at, expires_at, request_id) VALUES"
                     " (?, ?, ?, ?, ?, 'approved', ?, ?, ?)",
                     (refresh_id, company, start, end, reason, created, expires, request_id(rid)))
    log.info("refresh request approved %s", kv(refresh_id=refresh_id))
    return {"refresh_id": refresh_id, "status": "approved", "company": company,
            "start": start, "end": end, "expires_at": expires}


def expire(conn, clock: Clock) -> int:
    return conn.execute("UPDATE refresh_requests SET status = 'expired'"
                        " WHERE status = 'approved' AND expires_at < ?",
                        (clock.iso_now(),)).rowcount


def list_requests(store, status: str, refresh_id: str | None, limit: int, clock: Clock,
                  company_vis: Visibility) -> list[dict]:
    with store.write() as conn:            # the lazy expiry is a write
        expired = expire(conn, clock)
        sql, args = "SELECT * FROM refresh_requests WHERE 1=1", []
        if status != "all":
            sql += " AND status = ?"
            args.append(status)
        if refresh_id:
            sql += " AND id = ?"
            args.append(refresh_id)
        # A request for one company answers to that company's visibility;
        # "all" names no company and stays visible. Inside the SQL, before
        # the LIMIT, so denied requests can never push a visible one off a
        # short page (every other read filters the same way).
        sql += (" AND (company = 'all' OR (1=1"
                + visibility_clause("company", company_vis, args) + "))")
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        found = conn.execute(sql, args).fetchall()
    if expired:
        log.info("refresh requests expired %s", kv(count=expired))
    return [rows.refresh_request(r) for r in found]


def pending_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM refresh_requests"
                        " WHERE status IN ('approved', 'running')").fetchone()[0]


def report(store, params: dict, clock: Clock) -> dict:
    refresh_id = p.text(params, "refresh_id", required=True, min_len=1, max_len=64)
    status = p.choice(params, "status", ("running", "completed", "failed"), default="")
    if not status:
        raise AdapterError(400, "status is required")
    message = p.text(params, "message", default="", max_len=500)
    run_ids = params.get("run_ids")
    if run_ids is None:
        run_ids = []
    if not isinstance(run_ids, list) or len(run_ids) > MAX_RUN_IDS or not all(
            isinstance(r, str) and RUN_ID_RE.fullmatch(r) for r in run_ids):
        raise AdapterError(400, f"run_ids must be at most {MAX_RUN_IDS} run ids")
    now = clock.iso_now()
    with store.write() as conn:
        expire(conn, clock)
        if status == "running":
            cur = conn.execute("UPDATE refresh_requests SET status = 'running', claimed_at = ?,"
                               " message = ? WHERE id = ? AND status = 'approved'",
                               (now, message, refresh_id))
        else:
            cur = conn.execute("UPDATE refresh_requests SET status = ?, finished_at = ?,"
                               " message = ?, run_ids = ? WHERE id = ? AND status = 'running'",
                               (status, now, message, json.dumps(run_ids), refresh_id))
        row = conn.execute("SELECT * FROM refresh_requests WHERE id = ?",
                           (refresh_id,)).fetchone()
        if row is None:
            raise AdapterError(404, "no such refresh request")
        if cur.rowcount != 1:
            need = "approved" if status == "running" else "running"
            raise AdapterError(409, f"refresh request is {row['status']}, not {need}")
    log.info("refresh request reported %s", kv(refresh_id=refresh_id
                                               if REFRESH_ID_RE.fullmatch(refresh_id) else None,
                                               status=status))
    return rows.refresh_request(row)
