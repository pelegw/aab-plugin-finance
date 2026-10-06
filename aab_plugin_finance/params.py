"""Typed, fail-closed readers for action params.

The broker validates params against the manifest before they arrive, but the
adapter does not rely on that. Anything that holds the plugin token can reach
the plugin API, and tests call it directly. Every reader checks type and
bounds itself: bool is not an int, and a string must be valid UTF-8 text. A
failure gets a 400 with the param's NAME, never its value.

Absent and null are the same. The broker drops top-level nulls but keeps
nested ones (`source.balance_x100: null`), and both mean "not given".
"""

import re
from datetime import date
from typing import Any

from aab_plugin_runtime import AdapterError

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def text(params: dict, name: str, *, required: bool = False, default: str | None = None,
         max_len: int | None = None, min_len: int = 0) -> str | None:
    value = params.get(name)
    if value is None:
        if required:
            raise AdapterError(400, f"{name} is required")
        return default
    if not isinstance(value, str):
        raise AdapterError(400, f"{name} must be a string")
    try:
        # JSON can carry lone surrogates. SQLite would fail on them mid-write.
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise AdapterError(400, f"{name} is not valid text") from None
    if len(value) < min_len:
        raise AdapterError(400, f"{name} must be at least {min_len} characters")
    if max_len is not None and len(value) > max_len:
        raise AdapterError(400, f"{name} must be at most {max_len} characters")
    return value


def integer(params: dict, name: str, *, required: bool = False, default: int | None = None,
            minimum: int | None = None, maximum: int | None = None) -> int | None:
    value = params.get(name)
    if value is None:
        if required:
            raise AdapterError(400, f"{name} is required")
        return default
    # bool is an int subclass; True must not become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdapterError(400, f"{name} must be an integer")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise AdapterError(400, f"{name} is out of range")
    return value


def boolean(params: dict, name: str, *, default: bool) -> bool:
    value = params.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AdapterError(400, f"{name} must be true or false")
    return value


def choice(params: dict, name: str, values: tuple[str, ...], *, default: str) -> str:
    value = params.get(name)
    if value is None:
        return default
    if not isinstance(value, str) or value not in values:
        raise AdapterError(400, f"{name} must be one of {list(values)}")
    return value


def limit(params: dict, *, default: int, maximum: int) -> int:
    """Clamp the value into [1, maximum]. SQLite reads LIMIT -1 as "no limit",
    so a negative value must never reach a query."""
    value = integer(params, "limit")
    return max(1, min(default if value is None else value, maximum))


def is_date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def day(params: dict, name: str, *, required: bool = False) -> str | None:
    value = params.get(name)
    if value is None:
        if required:
            raise AdapterError(400, f"{name} is required")
        return None
    if not is_date(value):
        raise AdapterError(400, f"{name} must be a date, YYYY-MM-DD")
    return value


def month_bounds(value: str) -> tuple[str, str]:
    """('YYYY-MM-01', 'YYYY-MM-<last>') for a YYYY-MM month."""
    year, month = int(value[:4]), int(value[5:7])
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return f"{value}-01", date.fromordinal(nxt.toordinal() - 1).isoformat()


def period(params: dict, *, allow_month: bool = False,
           allow_year: bool = False) -> tuple[str | None, str | None, int | None]:
    """(start, end, year) from start/end, or month, or year (one way only)."""
    start, end = day(params, "start"), day(params, "end")
    month = params.get("month") if allow_month else None
    year = integer(params, "year", minimum=2000, maximum=2100) if allow_year else None
    if month is not None:
        if not isinstance(month, str) or not _MONTH_RE.fullmatch(month):
            raise AdapterError(400, "month must be YYYY-MM")
        if start or end:
            raise AdapterError(400, "give month or start/end, not both")
        start, end = month_bounds(month)
    if year is not None:
        if start or end:
            raise AdapterError(400, "give year or start/end, not both")
        start, end = f"{year}-01-01", f"{year}-12-31"
    if start and end and start > end:
        raise AdapterError(400, "start must not be after end")
    return start, end, year
