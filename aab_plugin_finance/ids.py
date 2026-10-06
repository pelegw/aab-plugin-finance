"""Canonical ids for the finance plugin's resource kinds.

The broker compares ids as plain strings (hidden resources, key denies,
capability selectors), so every id the owner or an agent types goes through
one of these normalizers first: two spellings of one card must never be two
ids, or hiding "Cal 1234" would leave "cal:1234" visible. Anything that does
not normalize is a 400. Error messages say what was expected and never echo
the value, which is caller input.

  card / account   "<company>:<digits>"   "Cal 1234", "CAL-1234" -> "cal:1234"
  company          "[a-z][a-z0-9]{1,31}"  "Cal" -> "cal"
  transaction      "tx_" + 16 hex          derived from the scraper's row key
"""

import hashlib
import re

from aab_plugin_runtime import AdapterError

COMPANY_RE = re.compile(r"[a-z][a-z0-9]{1,31}")
# A company, one separator (":", "-", "_" or spaces) and 1..8 digits. The
# separator is required: "cal1234" is ambiguous (company "cal1"?) and refused.
_SOURCE_RE = re.compile(r"\s*([A-Za-z][A-Za-z0-9]*)\s*[:_\s-]\s*([0-9]{1,8})\s*")
TX_ID_RE = re.compile(r"tx_[0-9a-f]{16}")

# Display names for the companies cred-analysis scrapes; anything else (a
# bank id) is shown as its id.
COMPANY_LABELS = {"cal": "Cal", "max": "Max", "isracard": "Isracard", "amex": "Amex"}


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise AdapterError(400, f"{what} must be a string")
    return value


def normalize_company(value: object) -> str:
    v = _text(value, "company").strip().lower()
    if not COMPANY_RE.fullmatch(v):
        raise AdapterError(400, "company must be a lowercase id like cal, max, isracard or amex")
    return v


def normalize_source_id(value: object) -> str:
    m = _SOURCE_RE.fullmatch(_text(value, "card or account id"))
    company = m.group(1).lower() if m else ""
    if not m or not COMPANY_RE.fullmatch(company):
        raise AdapterError(400, "card or account id must look like <company>:<digits>, "
                                "e.g. cal:1234")
    return f"{company}:{m.group(2)}"


def normalize_tx_id(value: object) -> str:
    v = _text(value, "transaction id").strip().lower()
    if not TX_ID_RE.fullmatch(v):
        raise AdapterError(400, "transaction id must be tx_ followed by 16 hex characters")
    return v


def tx_id(key: str) -> str:
    """The transaction's public id: a hash of the scraper's row key, so the
    key itself (card, amount, description) never has to leave the plugin."""
    return "tx_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def company_label(company: str) -> str:
    return COMPANY_LABELS.get(company, company)


def label_for(source_id: str, company: str, label: str = "") -> str:
    """What the owner sees for a card or account: its label when the scraper
    sent one, else "Cal 1234"."""
    if label:
        return label
    digits = source_id.split(":", 1)[1] if ":" in source_id else source_id
    return f"{company_label(company)} {digits}"
