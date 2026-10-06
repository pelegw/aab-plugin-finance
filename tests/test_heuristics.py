"""The analysis.js classification rules, including the JavaScript semantics
they depend on (ASCII word boundaries, JavaScript's whitespace set)."""

import math

import pytest

from aab_plugin_finance.heuristics import (cv_from_sums, is_foreign, is_transfer, merchant_key,
                                           normalize_description)


@pytest.mark.parametrize("description,expected", [
    ("העברה לחשבון", True), ("העברות", True), ("BIT", True), ("bit payment", True),
    ("Bit", True), ("PAY-BIT-X", True), ("ביט", True), ("תשלום ביט", True), ("ביט-העברה", True),
    ("הלוואה", True), ("לקרדיט", True), ("פירעון", True), ("החזר הלוואה", True),
    ("משיכת מזומן", True), ("CASH ADVANCE", True), ("cash advance fee", True),
    ("ביטוח רכב", False),             # insurance, not Bit
    ("מוביט", False),                 # Moovit
    ("HABIT STORE", False), ("BITS AND BOBS", False), ("ORBIT", False),
    ("ביטBIT", True),                 # JS \b: a Hebrew letter is not a word character
    ("_BIT", False), ("BIT9", False), ("SUPERMARKET", False), ("", False),
    ("bıt", False),                   # dotless i: JavaScript's /i does not fold it to I
])
def test_transfer(description, expected):
    assert is_transfer(description) is expected


@pytest.mark.parametrize("currency,expected", [
    ("", False), ("ILS", False), ("ils", False), ("NIS", False), ("₪", False),
    ("USD", True), ("EUR", True), ("eur", True)])
def test_foreign(currency, expected):
    assert is_foreign(currency) is expected


def test_descriptions_use_javascripts_whitespace():
    assert normalize_description("  Super \t\n Pharm  ") == "Super Pharm"
    assert normalize_description("a﻿b") == "a b"            # JS \s includes U+FEFF
    assert normalize_description("a\x1cb") == "a\x1cb"           # ...but not \x1c (Python's does)
    assert normalize_description(None) == ""
    assert merchant_key("  Super   PHARM ") == "super pharm"


@pytest.mark.parametrize("values", [[1990] * 4, [10000, 10000, 10000, 11500],
                                    [30000, 25000, 35000, 15000], [5], []])
def test_cv_matches_the_population_formula(values):
    n = len(values)
    if n < 2:
        expected = 0.0
    else:
        mean = sum(values) / n
        expected = math.sqrt(sum((x - mean) ** 2 for x in values) / n) / mean
    got = cv_from_sums(n, sum(values), sum(x * x for x in values))
    assert got == pytest.approx(expected, abs=1e-12)
    if values and len(set(values)) == 1:
        assert got == 0.0                                        # exactly, not a float residue
