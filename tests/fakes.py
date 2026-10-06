"""Test data in the scraper's own shape, seeded through the real action.

`txn(...)` is one transaction as israeli-bank-scrapers returns it (the shape
in cred-analysis data/raw/*.json); `make_rows` runs those through the same
normalizeAccount port the replay tool uses (tools/replay_raw.py), so rows
carry exactly the keys the real uploader produces; `ingest` uploads them
with `ingest_snapshot` through the plugin runtime, chunked, as the scraper
would. Nothing writes the database directly.
"""

import uuid

from tools.replay_raw import normalize_account, to_ingest_row

from .conftest import data

WIDE = ("2025-01-01", "2026-10-31")      # a range covering every seeded date
SCRAPED_AT = "2026-10-06T05:00:00.000Z"


def txn(date: str, amount: float, description: str, *, currency: str = "ILS",
        charged: float | None = None, charged_currency: str = "ILS", category: str = "",
        installments: tuple[int, int] | None = None, status: str = "completed",
        identifier: str = "", memo: str = "", processed: str | None = None) -> dict:
    return {"date": date, "processedDate": processed or date, "originalAmount": amount,
            "originalCurrency": currency,
            "chargedAmount": amount if charged is None else charged,
            "chargedCurrency": charged_currency, "description": description,
            "category": category, "status": status, "identifier": identifier, "memo": memo,
            "type": "installments" if installments else "normal",
            "installments": {"number": installments[0], "total": installments[1]}
            if installments else None}


def make_rows(company: str, card: str, txns: list[dict],
              rng: tuple[str, str] | None = None) -> list[dict]:
    """normalizeAccount rows (camelCase, with `key`) for one account."""
    span = {"start": rng[0], "end": rng[1]} if rng else None
    _, rows = normalize_account(company, {"accountNumber": card, "txns": txns}, span, None)
    return rows


def ingest_rows(rows: list[dict]) -> list[dict]:
    return [to_ingest_row(r) for r in rows]


def chunk_params(company: str, source_id: str, rows: list[dict], *, index: int, total: int,
                 run_id: str, rng: tuple[str, str] = WIDE, kind: str = "card",
                 scraped_at: str = SCRAPED_AT, refresh_id: str = "", **source) -> dict:
    return {"run_id": run_id, "company": company,
            "source": {"kind": kind, "id": source_id, **source},
            "range": {"start": rng[0], "end": rng[1]},
            "chunk": {"index": index, "total": total}, "scraped_at": scraped_at,
            "refresh_id": refresh_id, "transactions": rows}


def ingest(perform, company: str, card: str, txns: list[dict], *, rng: tuple[str, str] = WIDE,
           chunk_size: int = 400, run_id: str | None = None, kind: str = "card",
           scraped_at: str = SCRAPED_AT, call_scope=None, refresh_id: str = "") -> dict:
    """Upload one account's transactions as one run; returns the last answer."""
    rows = ingest_rows(make_rows(company, card, txns, rng))
    run_id = run_id or str(uuid.uuid4())
    chunks = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)] or [[]]
    answer = None
    for index, part in enumerate(chunks, start=1):
        answer = data(perform("ingest_snapshot", chunk_params(
            company, f"{company}:{card}", part, index=index, total=len(chunks), run_id=run_id,
            rng=rng, kind=kind, scraped_at=scraped_at, refresh_id=refresh_id), call_scope))
    return answer


# ---- the standard dataset: two cards and one bank account ---------------------------

CAL, MAX, BANK = "cal:1234", "max:5678", "leumi:123456"

CAL_TXNS = [
    txn("2026-09-01", -123.45, "SUPER-PHARM", category="Pharmacy", identifier="9981"),
    txn("2026-09-15", -49.90, "NETFLIX", category="Streaming", memo="monthly plan"),
    txn("2026-10-02", 30.00, "REFUND SHOP", category="Shopping"),
]
MAX_TXNS = [
    txn("2026-08-20", -55.00, "AMAZON MKTPLACE", currency="USD", charged=-200.00,
        category="Shopping"),
    txn("2026-10-01", -500.00, "SECRET CLINIC", category="Health"),
]
BANK_TXNS = [
    txn("2026-09-10", -1000.00, "העברה לחשבון", category="Transfers"),
    txn("2025-01-05", -10.00, "OLD ROW"),
]


def seed(perform) -> None:
    ingest(perform, "cal", "1234", CAL_TXNS)
    ingest(perform, "max", "5678", MAX_TXNS)
    ingest(perform, "leumi", "123456", BANK_TXNS, kind="account")
