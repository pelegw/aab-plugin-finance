"""`ingest_snapshot`: chunked, idempotent uploads applied in one transaction.

One run = one card or account of one company over one [start, end] range,
uploaded in 1..N chunks of at most 500 rows (finance-plugin-plan.md 3.2).

Validation the manifest's schema cannot express, all 400 before any write:
at most 500 rows per chunk; `run_id` is [A-Za-z0-9._-]; `source.id`
normalizes to <company>:<digits> under the run's own company; every row
`date` is a real YYYY-MM-DD inside [range.start, range.end]; amounts fit
comfortably in SQLite's integers (so a SUM can never overflow into a 503).

Idempotency: each chunk is stored with sha256(canonical payload). The same
chunk again answers `duplicate`; the same index with other content is a
409; chunks may arrive in any order, and whichever completes 1..total
applies the run in the same transaction. A chunk for an applied run answers
`applied` again with the run's counts (a safe replay after a lost response).
A run still staging 24 hours after its first chunk is marked `abandoned`
(lazily, on the next upload or list_runs) and its payloads deleted.

Merge (cred-analysis `excel.js::mergeRows`): upsert the source; insert new
row keys with first_seen = the scrape's local date, or update every
scraper-owned column of known keys (first_seen and notes are never
touched), counting added / updated / unchanged; then delete pending rows of
this source inside [start, end] that this run did not contain. Rows outside
the range are never touched and settled rows are never deleted.

Hidden cards do not block uploads: the action is bound to the `company`
resource, and only the company's visibility applies here, so the record
stays complete while reads of a hidden card stay 404.

Logs carry the run id, chunk index, row counts and outcome; never a key,
an amount, a description or a source id.
"""

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from aab_plugin_runtime import AdapterError
from aab_plugin_runtime.logging_setup import kv

from . import params as p
from .clock import Clock
from .heuristics import is_foreign, is_transfer, merchant_key
from .ids import normalize_company, normalize_source_id, tx_id
from .scope import Visibility

log = logging.getLogger(__name__)

MAX_ROWS = 500
ABANDON_SECONDS = 24 * 3600
# 10^13 hundredths = 100 billion: far beyond any real amount, and 500 rows
# of it cannot overflow a 64-bit SUM.
MAX_ABS_X100 = 10 ** 13
_BATCH = 500                         # bound parameters per IN (...) query
MAX_INSTALLMENTS = 10_000
RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,64}")
REFRESH_ID_RE = re.compile(r"[A-Za-z0-9._-]{0,64}")

_TOP = frozenset({"run_id", "company", "source", "range", "chunk", "scraped_at",
                  "refresh_id", "transactions"})
_SOURCE = frozenset({"kind", "id", "label", "last4", "currency", "balance_x100", "balance_at"})
_ROW = frozenset({"key", "date", "processed_date", "description", "category", "original_x100",
                  "original_currency", "charged_x100", "charged_currency", "type",
                  "installment_number", "installment_total", "status", "identifier", "memo"})
# The scraper-owned columns compared to count a row as updated (mergeRows'
# SYNCED_FIELDS); first_seen and notes are not among them.
SYNCED = ("processed_date", "description", "category", "original_x100", "original_currency",
          "charged_x100", "charged_currency", "type", "installment_number",
          "installment_total", "status", "identifier", "memo")


@dataclass(frozen=True)
class Chunk:
    run_id: str
    company: str
    source: dict
    start: str
    end: str
    index: int
    total: int
    scraped_at: str
    refresh_id: str
    rows: list[dict]
    sha256: str


# ---- validation --------------------------------------------------------------------

def _no_extra(obj: dict, allowed: frozenset[str], where: str) -> None:
    if set(obj) - allowed:
        raise AdapterError(400, f"{where} has unknown fields")


def _object(params: dict, name: str) -> dict:
    value = params.get(name)
    if not isinstance(value, dict):
        raise AdapterError(400, f"{name} must be an object")
    return value


def _amount(raw: dict, name: str) -> int:
    value = p.integer(raw, name, required=True)
    if abs(value) > MAX_ABS_X100:
        raise AdapterError(400, f"{name} is out of range")
    return value


def _source(raw: dict, company: str) -> dict:
    _no_extra(raw, _SOURCE, "source")
    kind = p.choice(raw, "kind", ("card", "account"), default="")
    if not kind:
        raise AdapterError(400, "source.kind is required")
    source_id = normalize_source_id(p.text(raw, "id", required=True, min_len=3, max_len=64))
    if not source_id.startswith(company + ":"):
        raise AdapterError(400, "source.id must start with the run's company")
    balance = raw.get("balance_x100")
    if balance is not None:
        balance = _amount(raw, "balance_x100")
    return {"kind": kind, "id": source_id,
            "label": p.text(raw, "label", default="", max_len=80),
            "last4": p.text(raw, "last4", default="", max_len=8),
            "currency": p.text(raw, "currency", default="ILS", max_len=3),
            "balance_x100": balance,
            "balance_at": p.text(raw, "balance_at", default="", max_len=10)}


def _row(raw: dict, start: str, end: str) -> dict:
    if not isinstance(raw, dict):
        raise AdapterError(400, "must be an object")
    _no_extra(raw, _ROW, "the row")
    row_date = raw.get("date")
    if not p.is_date(row_date) or not start <= row_date <= end:
        raise AdapterError(400, "date must be a YYYY-MM-DD date inside the run's range")
    # Bounded so an absurd value is a 400 here, not an overflow inside SQLite.
    number = p.integer(raw, "installment_number", minimum=1, maximum=MAX_INSTALLMENTS)
    total = p.integer(raw, "installment_total", minimum=1, maximum=MAX_INSTALLMENTS)
    status = p.choice(raw, "status", ("completed", "pending"), default="")
    if not status:
        raise AdapterError(400, "status is required")
    return {"key": p.text(raw, "key", required=True, min_len=1, max_len=512),
            "date": row_date,
            "processed_date": p.text(raw, "processed_date", default="", max_len=10),
            "description": p.text(raw, "description", required=True, max_len=500),
            "category": p.text(raw, "category", default="", max_len=120),
            "original_x100": _amount(raw, "original_x100"),
            "original_currency": p.text(raw, "original_currency", required=True,
                                        min_len=1, max_len=3),
            "charged_x100": _amount(raw, "charged_x100"),
            "charged_currency": p.text(raw, "charged_currency", required=True,
                                       min_len=1, max_len=3),
            "type": p.text(raw, "type", default="normal", max_len=20),
            "installment_number": number, "installment_total": total, "status": status,
            "identifier": p.text(raw, "identifier", default="", max_len=64),
            "memo": p.text(raw, "memo", default="", max_len=500)}


def parse(params: dict) -> Chunk:
    """Validate and normalize one chunk; raise 400 on the first problem."""
    _no_extra(params, _TOP, "params")
    run_id = p.text(params, "run_id", required=True)
    if not RUN_ID_RE.fullmatch(run_id):
        raise AdapterError(400, "run_id must be 8..64 characters of A-Z a-z 0-9 . _ -")
    company = normalize_company(p.text(params, "company", required=True, max_len=32))
    source = _source(_object(params, "source"), company)
    rng = _object(params, "range")
    _no_extra(rng, frozenset({"start", "end"}), "range")
    start, end = p.day(rng, "start", required=True), p.day(rng, "end", required=True)
    if start > end:
        raise AdapterError(400, "range.start must not be after range.end")
    chunk = _object(params, "chunk")
    _no_extra(chunk, frozenset({"index", "total"}), "chunk")
    index = p.integer(chunk, "index", required=True, minimum=1, maximum=1000)
    total = p.integer(chunk, "total", required=True, minimum=1, maximum=1000)
    if index > total:
        raise AdapterError(400, "chunk.index must not exceed chunk.total")
    scraped_at = p.text(params, "scraped_at", default="", max_len=40)
    refresh_id = p.text(params, "refresh_id", default="", max_len=64)
    if not REFRESH_ID_RE.fullmatch(refresh_id):
        raise AdapterError(400, "refresh_id must be A-Z a-z 0-9 . _ - only")
    raw_rows = params.get("transactions")
    if not isinstance(raw_rows, list):
        raise AdapterError(400, "transactions must be an array")
    if len(raw_rows) > MAX_ROWS:
        raise AdapterError(400, f"at most {MAX_ROWS} transactions per chunk")
    rows = []
    for i, raw in enumerate(raw_rows):
        try:
            rows.append(_row(raw, start, end))
        except AdapterError as exc:
            # The index and the rule, never the row's content.
            raise AdapterError(400, f"transactions[{i}]: {exc.message}") from None
    canonical = {"run_id": run_id, "company": company, "source": source,
                 "range": {"start": start, "end": end}, "chunk": {"index": index, "total": total},
                 "scraped_at": scraped_at, "refresh_id": refresh_id, "transactions": rows}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode("utf-8")).hexdigest()
    return Chunk(run_id, company, source, start, end, index, total, scraped_at, refresh_id,
                 rows, digest)


# ---- staging -----------------------------------------------------------------------

def stage(store, chunk: Chunk, clock: Clock, company_vis: Visibility) -> dict:
    """Store one chunk; apply the run when it completes the set."""
    company_vis.check_named(chunk.company, "company")
    with store.write() as conn:
        abandoned = sweep_abandoned(conn, clock)
        run = conn.execute("SELECT * FROM ingest_runs WHERE run_id = ?",
                           (chunk.run_id,)).fetchone()
        if run is not None and run["status"] == "applied":
            result = _applied(run)
        else:
            result = _stage_new_chunk(conn, run, chunk, clock)
    # Logged after COMMIT, so a line never reports what a rollback undid.
    fresh = result.pop("_fresh", False)
    log_abandoned(abandoned)
    log.info("ingest chunk %s", kv(run_id=chunk.run_id, chunk=chunk.index, total=chunk.total,
                                   rows=len(chunk.rows), status=result["status"]))
    if fresh:
        log.info("ingest run applied %s", kv(
            run_id=chunk.run_id, chunks=chunk.total, added=result["added"],
            updated=result["updated"], unchanged=result["unchanged"],
            removed_pending=result["removed_pending"]))
    return result


def _stage_new_chunk(conn: sqlite3.Connection, run, chunk: Chunk, clock: Clock) -> dict:
    if run is not None:
        if run["status"] == "abandoned":
            raise AdapterError(409, "this run was abandoned; upload it again under a new run_id")
        if (run["company"], run["source_id"], run["source_kind"], run["range_start"],
                run["range_end"], run["chunks_total"], run["refresh_id"] or "",
                run["scraped_at"] or "") != (
                chunk.company, chunk.source["id"], chunk.source["kind"], chunk.start, chunk.end,
                chunk.total, chunk.refresh_id, chunk.scraped_at):
            raise AdapterError(409, "this chunk does not match the run it names")
        seen = conn.execute("SELECT sha256 FROM ingest_chunks WHERE run_id = ? AND idx = ?",
                            (chunk.run_id, chunk.index)).fetchone()
        if seen is not None:
            if seen["sha256"] != chunk.sha256:
                raise AdapterError(409, f"chunk {chunk.index} was already received with "
                                        "different content")
            received = _received(conn, chunk.run_id)
            return {"status": "duplicate", "run_id": chunk.run_id, "received": received,
                    "missing": _missing(received, chunk.total)}
    else:
        conn.execute(
            "INSERT INTO ingest_runs (run_id, company, source_id, source_kind, range_start,"
            " range_end, chunks_total, status, refresh_id, scraped_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'staging', ?, ?, ?)",
            (chunk.run_id, chunk.company, chunk.source["id"], chunk.source["kind"], chunk.start,
             chunk.end, chunk.total, chunk.refresh_id, chunk.scraped_at, clock.iso_now()))
    payload = json.dumps({"source": chunk.source, "transactions": chunk.rows},
                         separators=(",", ":"), ensure_ascii=False)
    conn.execute("INSERT INTO ingest_chunks (run_id, idx, sha256, payload, received_at)"
                 " VALUES (?, ?, ?, ?, ?)",
                 (chunk.run_id, chunk.index, chunk.sha256, payload, clock.iso_now()))
    received = _received(conn, chunk.run_id)
    missing = _missing(received, chunk.total)
    if missing:
        return {"status": "staged", "run_id": chunk.run_id, "received": received,
                "missing": missing}
    counts = apply_run(conn, chunk.run_id, clock)
    return {"status": "applied", "run_id": chunk.run_id, **counts, "_fresh": True}


def _received(conn: sqlite3.Connection, run_id: str) -> list[int]:
    return [r["idx"] for r in conn.execute(
        "SELECT idx FROM ingest_chunks WHERE run_id = ? ORDER BY idx", (run_id,))]


def _missing(received: list[int], total: int) -> list[int]:
    have = set(received)
    return [i for i in range(1, total + 1) if i not in have]


def _applied(run) -> dict:
    return {"status": "applied", "run_id": run["run_id"], "added": run["added"],
            "updated": run["updated"], "unchanged": run["unchanged"],
            "removed_pending": run["removed_pending"]}


def log_abandoned(count: int) -> None:
    """Called after the sweep's transaction committed."""
    if count:
        log.info("ingest runs abandoned %s", kv(count=count))


def sweep_abandoned(conn: sqlite3.Connection, clock: Clock) -> int:
    """Mark runs staging for over 24 hours abandoned and drop their payloads."""
    cutoff = clock.iso_now(-ABANDON_SECONDS)
    ids = [r["run_id"] for r in conn.execute(
        "SELECT run_id FROM ingest_runs WHERE status = 'staging' AND created_at < ?", (cutoff,))]
    for batch in _batches(ids):
        marks = ",".join("?" for _ in batch)
        conn.execute(f"UPDATE ingest_runs SET status = 'abandoned' WHERE run_id IN ({marks})",
                     batch)
        conn.execute(f"DELETE FROM ingest_chunks WHERE run_id IN ({marks})", batch)
    return len(ids)


# ---- apply -------------------------------------------------------------------------

def apply_run(conn: sqlite3.Connection, run_id: str, clock: Clock) -> dict:
    """Merge a complete run, inside the caller's transaction."""
    run = conn.execute("SELECT * FROM ingest_runs WHERE run_id = ?", (run_id,)).fetchone()
    source, rows = None, {}
    for chunk in conn.execute("SELECT payload FROM ingest_chunks WHERE run_id = ? ORDER BY idx",
                              (run_id,)):
        data = json.loads(chunk["payload"])
        source = source or data["source"]          # chunk 1's source wins
        for row in data["transactions"]:
            rows[row["key"]] = row                 # a key repeated across chunks: the later chunk
    seen_date = clock.local_date(run["scraped_at"] or "")
    _upsert_source(conn, source, run["company"], seen_date, run_id)
    keys = list(rows)
    for batch in _batches(keys):
        marks = ",".join("?" for _ in batch)
        clash = conn.execute(f"SELECT 1 FROM transactions WHERE key IN ({marks})"
                             " AND source_id != ? LIMIT 1", [*batch, source["id"]]).fetchone()
        if clash is not None:
            raise AdapterError(409, "a row key in this run already belongs to another card "
                                    "or account")
    existing: dict[str, sqlite3.Row] = {}
    for batch in _batches(keys):
        marks = ",".join("?" for _ in batch)
        for r in conn.execute(f"SELECT key, {', '.join(SYNCED)} FROM transactions"
                              f" WHERE key IN ({marks})", batch):
            existing[r["key"]] = r
    added = updated = unchanged = 0
    for key, row in rows.items():
        derived = {"merchant_key": merchant_key(row["description"]),
                   "is_transfer": int(is_transfer(row["description"])),
                   "is_foreign": int(is_foreign(row["original_currency"]))}
        old = existing.get(key)
        if old is None:
            _insert(conn, row, derived, source, run["company"], seen_date, run_id)
            added += 1
            continue
        if any(old[f] != row[f] for f in SYNCED):
            updated += 1
        else:
            unchanged += 1
        _update(conn, row, derived, source, run["company"], seen_date, run_id)
    removed = conn.execute(
        "DELETE FROM transactions WHERE source_id = ? AND status = 'pending'"
        " AND date BETWEEN ? AND ? AND last_seen_run_id != ?",
        (source["id"], run["range_start"], run["range_end"], run_id)).rowcount
    now = clock.iso_now()
    conn.execute("UPDATE ingest_runs SET status = 'applied', applied_at = ?, added = ?,"
                 " updated = ?, unchanged = ?, removed_pending = ? WHERE run_id = ?",
                 (now, added, updated, unchanged, removed, run_id))
    conn.execute("DELETE FROM ingest_chunks WHERE run_id = ?", (run_id,))
    conn.execute("INSERT INTO meta (key, value) VALUES ('last_ingest_at', ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (now,))
    return {"added": added, "updated": updated, "unchanged": unchanged,
            "removed_pending": removed}


def _upsert_source(conn: sqlite3.Connection, source: dict, company: str, seen: str,
                   run_id: str) -> None:
    old = conn.execute("SELECT kind FROM sources WHERE id = ?", (source["id"],)).fetchone()
    if old is None:
        conn.execute(
            "INSERT INTO sources (id, kind, company, label, last4, currency, balance_x100,"
            " balance_at, first_seen, last_seen, last_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source["id"], source["kind"], company, source["label"], source["last4"],
             source["currency"], source["balance_x100"], source["balance_at"] or None, seen,
             seen, run_id))
        return
    if old["kind"] != source["kind"]:
        raise AdapterError(409, "this id is already known as another kind of source")
    # An upload that leaves label/last4/balance empty keeps what is known.
    conn.execute(
        "UPDATE sources SET label = CASE WHEN ? != '' THEN ? ELSE label END,"
        " last4 = CASE WHEN ? != '' THEN ? ELSE last4 END, currency = ?,"
        " balance_x100 = COALESCE(?, balance_x100),"
        " balance_at = CASE WHEN ? IS NOT NULL THEN ? ELSE balance_at END,"
        " last_seen = ?, last_run_id = ? WHERE id = ?",
        (source["label"], source["label"], source["last4"], source["last4"], source["currency"],
         source["balance_x100"], source["balance_x100"], source["balance_at"] or None, seen,
         run_id, source["id"]))


_COLUMNS = ("source_kind", "company", "date", "processed_date", "description", "merchant_key",
            "category", "original_x100", "original_currency", "charged_x100", "charged_currency",
            "type", "installment_number", "installment_total", "status", "identifier", "memo",
            "is_transfer", "is_foreign", "last_seen", "last_seen_run_id")


def _values(row: dict, derived: dict, source: dict, company: str, seen: str,
            run_id: str) -> list:
    merged = {**row, **derived, "source_kind": source["kind"], "company": company,
              "last_seen": seen, "last_seen_run_id": run_id}
    return [merged[c] for c in _COLUMNS]


def _insert(conn, row, derived, source, company, seen, run_id) -> None:
    cols = ("id", "key", "source_id", "first_seen") + _COLUMNS
    conn.execute(f"INSERT INTO transactions ({', '.join(cols)})"
                 f" VALUES ({', '.join('?' for _ in cols)})",
                 [tx_id(row["key"]), row["key"], source["id"], seen,
                  *_values(row, derived, source, company, seen, run_id)])


def _update(conn, row, derived, source, company, seen, run_id) -> None:
    conn.execute(f"UPDATE transactions SET {', '.join(c + ' = ?' for c in _COLUMNS)}"
                 " WHERE key = ?",
                 [*_values(row, derived, source, company, seen, run_id), row["key"]])


def _batches(items: list, size: int = _BATCH):
    for i in range(0, len(items), size):
        yield items[i:i + size]
