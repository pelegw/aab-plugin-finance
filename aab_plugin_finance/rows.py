"""Result shaping: database rows to the JSON that agents receive.

The database keeps amounts in integer hundredths. Results show them as numbers
in units (`charged_x100 = -12345` -> `charged_amount: -123.45`). Every row that
names a card or an account carries `resource_ref: {kind, id}`. The broker's
own post-filter can then drop the row if this plugin ever lets a hidden one
through.

The grant can ask for these redactions. This module applies them, and no other
module does:
  * `merchant_names: false`: the description becomes an opaque merchant id
    (`merchant:` + 10 hex of sha256(merchant_key)). The id is stable per
    merchant, so totals still group. The memo becomes empty. Categories stay.
  * `notes: false`: the `note` field is absent, not null. Thus not even the
    presence of a note shows.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def amount(x100: int | None) -> float | None:
    return None if x100 is None else round(x100 / 100, 2)


def ratio(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def source_ref(kind: str, source_id: str) -> dict:
    return {"kind": kind, "id": source_id}


def merchant_token(merchant_key: str) -> str:
    return "merchant:" + hashlib.sha256(merchant_key.encode("utf-8")).hexdigest()[:10]


def merchant_name(name: str, merchant_key: str, names: bool) -> str:
    return name if names else merchant_token(merchant_key)


def transaction(row: Mapping[str, Any], *, notes: bool, names: bool) -> dict:
    installment = None
    if row["installment_total"] is not None or row["installment_number"] is not None:
        installment = {"number": row["installment_number"], "total": row["installment_total"]}
    out = {
        "id": row["id"],
        "source": source_ref(row["source_kind"], row["source_id"]),
        "company": row["company"],
        "date": row["date"],
        "processed_date": row["processed_date"],
        "description": merchant_name(row["description"], row["merchant_key"], names),
        "category": row["category"],
        "original_amount": amount(row["original_x100"]),
        "original_currency": row["original_currency"],
        "charged_amount": amount(row["charged_x100"]),
        "charged_currency": row["charged_currency"],
        "type": row["type"],
        "installment": installment,
        "status": row["status"],
        "identifier": row["identifier"],
        "memo": row["memo"] if names else "",
    }
    if notes:
        keys = row.keys() if hasattr(row, "keys") else ()
        out["note"] = row["note"] if "note" in keys else None
    out["first_seen"] = row["first_seen"]
    out["last_seen"] = row["last_seen"]
    out["resource_ref"] = source_ref(row["source_kind"], row["source_id"])
    return out


def source(row: Mapping[str, Any], label: str) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "company": row["company"], "label": label,
        "last4": row["last4"], "currency": row["currency"],
        "balance": amount(row["balance_x100"]), "balance_at": row["balance_at"] or "",
        "count": row["tx_count"], "first_date": row["first_date"], "last_date": row["last_date"],
        "first_seen": row["first_seen"], "last_seen": row["last_seen"],
        "last_run_at": row["last_run_at"],
        "resource_ref": source_ref(row["kind"], row["id"]),
    }


def run(row: Mapping[str, Any]) -> dict:
    """Run metadata. It contains no transaction content."""
    return {
        "run_id": row["run_id"], "company": row["company"],
        "source": source_ref(row["source_kind"], row["source_id"]),
        "range": {"start": row["range_start"], "end": row["range_end"]},
        "status": row["status"], "chunks_total": row["chunks_total"],
        "chunks_received": row["chunks_received"],
        "refresh_id": row["refresh_id"] or "", "scraped_at": row["scraped_at"] or "",
        "created_at": row["created_at"], "applied_at": row["applied_at"],
        "added": row["added"], "updated": row["updated"], "unchanged": row["unchanged"],
        "removed_pending": row["removed_pending"],
        "resource_ref": source_ref(row["source_kind"], row["source_id"]),
    }


def refresh_request(row: Mapping[str, Any]) -> dict:
    try:
        run_ids = json.loads(row["run_ids"] or "[]")
    except ValueError:
        run_ids = []
    return {
        "refresh_id": row["id"], "company": row["company"],
        "start": row["range_start"], "end": row["range_end"], "reason": row["reason"],
        "status": row["status"], "created_at": row["created_at"],
        "expires_at": row["expires_at"], "claimed_at": row["claimed_at"],
        "finished_at": row["finished_at"], "message": row["message"],
        "run_ids": run_ids if isinstance(run_ids, list) else [],
    }
