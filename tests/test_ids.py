"""Canonical ids: one spelling per card, account, company and transaction."""

import hashlib

import pytest
from aab_plugin_runtime import AdapterError

from aab_plugin_finance.ids import (label_for, normalize_company, normalize_source_id,
                                    normalize_tx_id, tx_id)


@pytest.mark.parametrize("raw,canonical", [
    ("cal:1234", "cal:1234"), ("Cal 1234", "cal:1234"), ("CAL-1234", "cal:1234"),
    ("cal_1234", "cal:1234"), ("  cal : 0042 ", "cal:0042"), ("leumi:123456", "leumi:123456"),
    ("isracard:12345678", "isracard:12345678"),
])
def test_source_ids(raw, canonical):
    assert normalize_source_id(raw) == canonical


@pytest.mark.parametrize("raw", ["cal1234", "cal:", ":1234", "c:1234", "cal:123456789",
                                 "cal:12a4", "cal::1234", "9cal:1234", "cal:١٢٣٤", "", 1234,
                                 None, "cal 12 34"])
def test_bad_source_ids_are_400(raw):
    with pytest.raises(AdapterError) as e:
        normalize_source_id(raw)
    # A fixed message: the value (caller input) is never echoed.
    assert e.value.status == 400 and e.value.message in (
        "card or account id must be a string",
        "card or account id must look like <company>:<digits>, e.g. cal:1234")


@pytest.mark.parametrize("raw,canonical", [("cal", "cal"), (" MAX ", "max"),
                                           ("isracard", "isracard"), ("bank1", "bank1")])
def test_companies(raw, canonical):
    assert normalize_company(raw) == canonical


@pytest.mark.parametrize("raw", ["c", "1cal", "cal ltd", "x" * 33, "", None])
def test_bad_companies_are_400(raw):
    with pytest.raises(AdapterError):
        normalize_company(raw)


def test_tx_ids():
    key = "cal|1234|9981|2026-03-04|-123.45|ILS|super-pharm|#1"
    assert tx_id(key) == "tx_" + hashlib.sha256(key.encode()).hexdigest()[:16]
    assert normalize_tx_id(tx_id(key).upper().replace("TX_", "tx_")) == tx_id(key)
    for bad in ("tx_123", "tx_0123456789abcdeg", "0123456789abcdef", "tx-0123456789abcdef"):
        with pytest.raises(AdapterError):
            normalize_tx_id(bad)


def test_labels():
    assert label_for("cal:1234", "cal") == "Cal 1234"
    assert label_for("leumi:123456", "leumi") == "leumi 123456"
    assert label_for("cal:1234", "cal", "Family card") == "Family card"
