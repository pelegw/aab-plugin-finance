"""The classification rules of cred-analysis `src/analysis.js`, ported verbatim.

The owner's workbook numbers come from analysis.js. The plugin must classify
exactly the same way, or its totals would not match the workbook:
  * merchantKey(s): normalizeDescription(s).toLowerCase(). It collapses
    whitespace runs to one space, trims the text and lowercases it.
  * TRANSFER: money movements (transfers, Bit, loans, cash advance). The
    regex matches them against the description.
  * ILS: {'', 'ILS', 'NIS', '₪'}. An original currency outside this set, in
    uppercase, is foreign.
  * cv(xs): population standard deviation / mean. It is 0 below two values
    or at a zero mean.

This code reproduces two JavaScript semantics on purpose, instead of
Python's:
  * `\\s` and `trim()` use JavaScript's whitespace set: no \\x1c-\\x1f or
    \\x85, plus U+FEFF.
  * The regex's `\\b` is JavaScript's ASCII word boundary. A Hebrew letter is
    not a "word" character there, so "ביטBIT" still has a boundary before
    "BIT".
Python's defaults differ on both.
"""

import math
import re

# JavaScript's \s: WhiteSpace and LineTerminator code points.
_JS_SPACE = "\t\n\v\f\r    -     　﻿"
_JS_SPACES = re.compile(f"[{_JS_SPACE}]+")
_JS_TRIM = re.compile(f"^[{_JS_SPACE}]+|[{_JS_SPACE}]+$")
# JavaScript's \b with no `u` flag: a boundary between [A-Za-z0-9_] and anything else.
_B_START = r"(?<![A-Za-z0-9_])"
_B_END = r"(?![A-Za-z0-9_])"

def _ascii_ci(word: str) -> str:
    """Case-insensitive for ASCII letters only, as JavaScript's `i` flag is in
    practice for these words. Python's IGNORECASE would also fold other
    letters into them, for example the dotless i and the Kelvin sign."""
    return "".join(f"[{c.upper()}{c.lower()}]" if c.isalpha() else re.escape(c) for c in word)


# analysis.js:
# /העברה|העברות|\bBIT\b|(^|[\s-])ביט([\s-]|$)|הלוואה|לקרדיט|פירעון|החזר הלוואה|משיכת מזומן|CASH ADVANCE/i
# "ביט" (Bit, the payment app) must stand alone: "ביטוח" (insurance) and "מוביט"
# (Moovit) are purchases. Hebrew has no case, so the `i` flag only ever
# mattered for the two Latin words.
TRANSFER = re.compile(
    "העברה|העברות|" + _B_START + _ascii_ci("BIT") + _B_END
    + f"|(^|[{_JS_SPACE}-])ביט([{_JS_SPACE}-]|$)"
    + "|הלוואה|לקרדיט|פירעון|החזר הלוואה|משיכת מזומן|" + _ascii_ci("CASH ADVANCE"))

ILS = frozenset({"", "ILS", "NIS", "₪"})


def normalize_description(s: object) -> str:
    """String(s ?? '').replace(/\\s+/g, ' ').trim()"""
    text = "" if s is None else str(s)
    return _JS_TRIM.sub("", _JS_SPACES.sub(" ", text))


def merchant_key(description: str) -> str:
    return normalize_description(description).lower()


def is_transfer(description: str) -> bool:
    return TRANSFER.search(description or "") is not None


def is_foreign(original_currency: str) -> bool:
    return (original_currency or "").upper() not in ILS


def cv_from_sums(n: int, total: int, squares: int) -> float:
    """cv() over n positive integers, from their sum and their sum of squares.

    The variance numerator (n*Σx² - (Σx)²) uses exact integer arithmetic, so
    identical amounts give exactly 0, not a float residue. The subscription
    test is `cv <= 0.15`, and it must not wobble at the edge."""
    if n < 2 or total == 0:
        return 0.0
    numerator = max(0, n * squares - total * total)
    # std = sqrt(numerator) / n and mean = total / n, so cv = sqrt(numerator) / total.
    return math.sqrt(numerator) / total
