#!/usr/bin/env python3
"""Replay cred-analysis raw snapshots into the finance plugin, through the broker.

The first bulk load, before the Mac uploader exists: reads the scraper's
`data/raw/*.json` snapshots in `scrapedAt` order (as cred-analysis
`import-raw.js` does), turns each account into rows exactly like
`normalize.js::normalizeAccount` (same stable row key, so a later upload of
the same transaction from the Mac merges with it instead of duplicating),
and uploads each account as one `ingest_snapshot` run with an agent key that
holds that action (key A):

  * chunks of 400 rows, sent in order, `X-Request-Id: <run_id>-<index>`;
  * 503 and 429 (not performed) and network errors: wait and retry, with
    exponential backoff that honours Retry-After;
  * 502 and 504 (outcome unknown): send the same chunk again; the plugin's
    chunk hash makes that safe (`duplicate`, or `applied` after the apply);
  * any other 4xx/5xx: stop, print the status and the broker's error code.

It prints file names, source ids, run ids and counts. It never prints a
transaction (descriptions, amounts, keys) or the key. Only the standard
library is used, so it runs from any Python 3.11+ (time zones: the machine's
local zone by default, which is what normalize.js used; --tz needs the
IANA database, e.g. `pip install tzdata` on Windows).

  AAB_KEY=aab_... python tools/replay_raw.py --raw ../cred-analysis/data/raw \\
      --url https://aab.example.com
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable

COMPANIES = {"cal": "Cal", "max": "Max", "isracard": "Isracard", "amex": "Amex"}
CHUNK_ROWS = 400
MAX_CHUNK_ROWS = 500
ACTION_PATH = "/v1/targets/finance/actions/ingest_snapshot"

# ---- JavaScript semantics normalize.js relies on --------------------------------------

_JS_SPACE = "\t\n\v\f\r    -     　﻿"
_JS_SPACES = re.compile(f"[{_JS_SPACE}]+")
_JS_TRIM = re.compile(f"^[{_JS_SPACE}]+|[{_JS_SPACE}]+$")
_YMD = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# normalize.js CURRENCY_CODES: companies disagree on currency spelling.
CURRENCY_CODES = {
    "₪": "ILS", "NIS": "ILS", 'ש"ח': "ILS", "ש״ח": "ILS", "שח": "ILS",
    "€": "EUR", "£": "GBP", "$": "USD", "US$": "USD", "¥": "JPY", "₩": "KRW", "₹": "INR",
    "₺": "TRY", "₽": "RUB",
}


def js_string(value: Any) -> str:
    """String(value) for the JSON types a snapshot holds."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    return str(value)


def js_trim(text: str) -> str:
    return _JS_TRIM.sub("", text)


def normalize_description(value: Any) -> str:
    """String(s ?? '').replace(/\\s+/g, ' ').trim()"""
    text = "" if value is None else js_string(value)
    return js_trim(_JS_SPACES.sub(" ", text))


def normalize_currency(raw: Any) -> str:
    s = js_trim("" if raw is None else js_string(raw))
    if not s:
        return ""
    return CURRENCY_CODES.get(s) or CURRENCY_CODES.get(s.upper()) or s.upper()


def num(value: Any) -> float:
    """normalize.js num(): Number(v) when finite, else 0."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else 0
    text = js_trim(str(value))
    if not text:
        return 0
    try:
        out = float(text)
    except ValueError:
        return 0
    return out if math.isfinite(out) else 0


def to_fixed2(value: float) -> str:
    """Number.prototype.toFixed(2): exact binary value, ties away from zero,
    "-0.00" for a tiny negative but "0.00" for zero itself."""
    if value == 0:
        return "0.00"
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def js_round(value: float) -> int:
    """Math.round: nearest integer, ties toward +infinity (-2.5 -> -2)."""
    d = Decimal(value)
    if d >= 0:
        return int(d.to_integral_value(rounding=ROUND_HALF_UP))
    return -int((-d).to_integral_value(rounding=ROUND_HALF_DOWN))


def last4(account_number: Any) -> str:
    raw = "" if account_number is None else js_string(account_number)
    digits = re.sub(r"[^0-9]", "", raw)
    return digits[-4:] or ("unknown" if account_number is None else js_string(account_number))


def iso_to_local_ymd(iso: Any, tz: tzinfo | None) -> str:
    """util.js isoToLocalYmd: the local calendar date of the library's ISO
    timestamp; `tz` None is this machine's zone, like `new Date()`."""
    if not iso:
        return ""
    s = js_string(iso)
    if _YMD.match(s):
        return s
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:10]
    if parsed.tzinfo is None:
        return parsed.date().isoformat()
    return parsed.astimezone(tz).date().isoformat()


def normalize_account(company: str, account: dict, rng: dict | None,
                      tz: tzinfo | None) -> tuple[str, list[dict]]:
    """normalize.js normalizeAccount: flat rows in range, each with its key."""
    card = last4(account.get("accountNumber"))
    out = []
    for t in account.get("txns") or []:
        day = iso_to_local_ymd(t.get("date"), tz)
        if not day:
            continue
        if rng and (day < rng["start"] or day > rng["end"]):
            continue
        installments = t.get("installments") if isinstance(t.get("installments"), dict) else {}
        charged_currency = t.get("chargedCurrency")
        out.append({
            "company": company, "card": card, "date": day,
            "processedDate": iso_to_local_ymd(t.get("processedDate"), tz),
            "description": normalize_description(t.get("description")),
            "category": normalize_description(t.get("category")),     # ?? '' is built in
            "originalAmount": num(t.get("originalAmount")),
            "originalCurrency": normalize_currency(t.get("originalCurrency")),
            "chargedAmount": num(t.get("chargedAmount")),
            "chargedCurrency": normalize_currency("ILS" if charged_currency is None
                                                  else charged_currency),
            "type": js_string(t.get("type") if t.get("type") is not None else "normal"),
            "installmentNumber": installments.get("number"),
            "installmentTotal": installments.get("total"),
            "status": js_string(t.get("status") if t.get("status") is not None else "completed"),
            "identifier": "" if t.get("identifier") is None else js_string(t.get("identifier")),
            "memo": normalize_description(t.get("memo")),
        })
    seen: dict[str, int] = {}
    for r in out:
        number = r["installmentNumber"]
        base = "|".join([r["company"], r["card"], r["identifier"], r["date"],
                         to_fixed2(r["originalAmount"]), r["originalCurrency"],
                         r["description"].lower(), "" if number is None else js_string(number)])
        seen[base] = seen.get(base, 0) + 1
        r["key"] = f"{base}#{seen[base]}"
    return card, out


def to_ingest_row(row: dict) -> dict:
    """One normalizeAccount row as an ingest_snapshot row (x100 with
    Math.round, nulls omitted). An empty currency means ILS to analysis.js;
    the plugin needs a code, so it is sent as ILS (the key keeps '')."""
    out = {
        "key": row["key"], "date": row["date"], "processed_date": row["processedDate"],
        "description": row["description"], "category": row["category"],
        "original_x100": js_round(row["originalAmount"] * 100),
        "original_currency": row["originalCurrency"] or "ILS",
        "charged_x100": js_round(row["chargedAmount"] * 100),
        "charged_currency": row["chargedCurrency"] or "ILS",
        "type": row["type"], "status": row["status"], "identifier": row["identifier"],
        "memo": row["memo"],
    }
    for name, value in (("installment_number", row["installmentNumber"]),
                        ("installment_total", row["installmentTotal"])):
        if value is not None:
            out[name] = int(value) if isinstance(value, float) and value.is_integer() else value
    return out


_LIMITS = {"key": (1, 512), "description": (0, 500), "category": (0, 120), "memo": (0, 500),
           "identifier": (0, 64), "type": (0, 20), "processed_date": (0, 10),
           "original_currency": (1, 3), "charged_currency": (1, 3)}


def row_problem(row: dict) -> str | None:
    """Why the plugin would refuse this row (a rule name, never content)."""
    for name, (low, high) in _LIMITS.items():
        if not low <= len(row[name]) <= high:
            return f"{name} length"
    if row["status"] not in ("completed", "pending"):
        return "status"
    for name in ("installment_number", "installment_total"):
        value = row.get(name)
        if value is not None and (not isinstance(value, int) or value < 1):
            return name
    return None


# ---- snapshots to runs -------------------------------------------------------------------

@dataclass
class Run:
    file: str
    company: str
    source_id: str
    label: str
    start: str
    end: str
    scraped_at: str
    rows: list[dict]
    skipped: dict[str, int] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def load_snapshots(raw_dir: Path) -> list[tuple[str, dict]]:
    """Snapshots import-raw.js would use, oldest scrape first."""
    out = []
    for path in sorted(raw_dir.glob("*.json")):
        snap = json.loads(path.read_text(encoding="utf-8"))
        rng = snap.get("range") or {}
        if snap.get("company") in COMPANIES and isinstance(snap.get("accounts"), list) \
                and rng.get("start") and rng.get("end"):
            out.append((path.name, snap))
    out.sort(key=lambda fs: js_string(fs[1].get("scrapedAt")))
    return out


def plan_runs(snapshots: list[tuple[str, dict]], tz: tzinfo | None) -> tuple[list[Run], list[str]]:
    """One run per account per snapshot; accounts with no usable card
    number are reported, not uploaded."""
    runs, problems = [], []
    for name, snap in snapshots:
        company = snap["company"]
        rng = {"start": snap["range"]["start"], "end": snap["range"]["end"]}
        for i, account in enumerate(snap["accounts"]):
            card, rows = normalize_account(company, account, rng, tz)
            if not re.fullmatch(r"[0-9]{1,8}", card):
                problems.append(f"{name}: account #{i + 1} has no card number; skipped")
                continue
            run = Run(name, company, f"{company}:{card}", f"{COMPANIES[company]} {card}",
                      rng["start"], rng["end"], js_string(snap.get("scrapedAt") or ""), [])
            for row in rows:
                ingest = to_ingest_row(row)
                problem = row_problem(ingest)
                if problem:
                    run.skipped[problem] = run.skipped.get(problem, 0) + 1
                else:
                    run.rows.append(ingest)
            runs.append(run)
    return runs, problems


def chunk_params(run: Run, index: int, total: int, rows: list[dict]) -> dict:
    return {"run_id": run.run_id, "company": run.company,
            "source": {"kind": "card", "id": run.source_id, "label": run.label,
                       "last4": run.source_id.split(":", 1)[1]},
            "range": {"start": run.start, "end": run.end},
            "chunk": {"index": index, "total": total},
            "scraped_at": run.scraped_at, "transactions": rows}


# ---- HTTP ------------------------------------------------------------------------------

class UploadError(Exception):
    """The broker refused a chunk (or kept failing): stop the replay."""

    def __init__(self, status: int | None, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


def _error_detail(body: bytes) -> str:
    """The broker's error code and message, which name rules, never values."""
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return "(no JSON body)"
    if not isinstance(data, dict):
        return "(unexpected body)"
    return " ".join(str(data[k]) for k in ("code", "error") if data.get(k)) or "(no detail)"


class Client:
    """POSTs chunks to the broker. `opener` and `sleep` are injectable for tests."""

    RETRY_NOT_PERFORMED = (429, 503)
    RESEND_UNKNOWN = (502, 504)

    def __init__(self, base_url: str, key: str, *, attempts: int = 6, timeout: float = 120,
                 opener: Callable = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep, max_wait: float = 60):
        self.url = base_url.rstrip("/") + ACTION_PATH
        self._key = key
        self.attempts, self.timeout, self.max_wait = attempts, timeout, max_wait
        self._open, self._sleep = opener, sleep

    def __repr__(self) -> str:                 # the key must never appear here
        return f"Client(url={self.url!r})"

    def post(self, params: dict, request_id: str) -> dict:
        body = json.dumps({"params": params}, ensure_ascii=False).encode("utf-8")
        last = "no attempt made"
        for attempt in range(1, self.attempts + 1):
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                "X-Request-Id": request_id, "Accept": "application/json"})
            wait = None
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                status, payload = exc.code, exc.read()
                if status in self.RETRY_NOT_PERFORMED or status in self.RESEND_UNKNOWN:
                    last = f"HTTP {status}"
                    wait = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                else:
                    raise UploadError(status, _error_detail(payload)) from None
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                # Not sent, or sent and the answer lost: either way the
                # chunk is safe to send again.
                last = type(exc).__name__
            if attempt < self.attempts:
                delay = wait if wait is not None else min(2 ** (attempt - 1), 30)
                self._sleep(min(delay, self.max_wait))
        raise UploadError(None, f"gave up after {self.attempts} attempts ({last})")


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def upload_run(client: Client, run: Run, chunk_rows: int = CHUNK_ROWS) -> dict:
    """Send every chunk of one run in order; return the `applied` answer."""
    chunks = [run.rows[i:i + chunk_rows] for i in range(0, len(run.rows), chunk_rows)] or [[]]
    answer: dict = {}
    for index, rows in enumerate(chunks, start=1):
        answer = client.post(chunk_params(run, index, len(chunks), rows),
                             f"{run.run_id}-{index}")
    if answer.get("status") != "applied":
        raise UploadError(None, f"run did not apply (last answer: {answer.get('status')})")
    return answer


# ---- command line ----------------------------------------------------------------------

def _zone(name: str | None) -> tzinfo | None:
    if not name:
        return None
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def _key(args) -> str:
    if args.key:
        return args.key
    if args.key_file:
        return Path(args.key_file).read_text(encoding="utf-8").strip()
    return os.environ.get("AAB_KEY", "").strip()


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise SystemExit("--url must be https:// (plain http only for localhost): "
                         "the agent key travels in every request")


def main(argv: list[str] | None = None, *, client_factory: Callable[..., Client] = Client,
         out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--raw", required=True, help="cred-analysis data/raw directory")
    ap.add_argument("--url", help="broker base URL, e.g. https://aab.example.com")
    ap.add_argument("--key", help="agent key (prefer AAB_KEY or --key-file: argv is visible)")
    ap.add_argument("--key-file", help="file holding the agent key")
    ap.add_argument("--tz", help="IANA zone for calendar dates (default: this machine's)")
    ap.add_argument("--chunk-rows", type=int, default=CHUNK_ROWS)
    ap.add_argument("--dry-run", action="store_true", help="plan and count, send nothing")
    args = ap.parse_args(argv)
    if not 1 <= args.chunk_rows <= MAX_CHUNK_ROWS:
        raise SystemExit(f"--chunk-rows must be 1..{MAX_CHUNK_ROWS}")
    runs, problems = plan_runs(load_snapshots(Path(args.raw)), _zone(args.tz))
    for line in problems:
        print(line, file=out)
    if args.dry_run:
        for run in runs:
            print(f"{run.file}: {run.source_id} {len(run.rows)} rows in {run.start}..{run.end}"
                  f"{_skipped(run)}", file=out)
        print(f"{len(runs)} runs planned, nothing sent", file=out)
        return 0
    if not args.url:
        raise SystemExit("--url is required (or use --dry-run)")
    _check_url(args.url)
    key = _key(args)
    if not key.startswith("aab_"):
        raise SystemExit("an agent key (aab_...) is required: AAB_KEY, --key-file or --key")
    client = client_factory(args.url, key)
    totals = {"added": 0, "updated": 0, "unchanged": 0, "removed_pending": 0}
    for run in runs:
        try:
            answer = upload_run(client, run, args.chunk_rows)
        except UploadError as exc:
            print(f"{run.file}: {run.source_id} run {run.run_id}: stopped, HTTP "
                  f"{exc.status if exc.status is not None else '-'} {exc.detail}", file=out)
            return 1
        for k in totals:
            totals[k] += int(answer.get(k) or 0)
        print(f"{run.file}: {run.source_id} run {run.run_id}: applied added={answer.get('added')}"
              f" updated={answer.get('updated')} unchanged={answer.get('unchanged')}"
              f" removed_pending={answer.get('removed_pending')}{_skipped(run)}", file=out)
    print(f"{len(runs)} runs applied: " + " ".join(f"{k}={v}" for k, v in totals.items()),
          file=out)
    return 0


def _skipped(run: Run) -> str:
    if not run.skipped:
        return ""
    return " skipped=" + ",".join(f"{k}:{v}" for k, v in sorted(run.skipped.items()))


if __name__ == "__main__":
    sys.exit(main())
