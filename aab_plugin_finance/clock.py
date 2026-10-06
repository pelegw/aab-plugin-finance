"""The plugin's notion of "now" and "today", in one injectable object.

Dates in the data are the owner's local calendar dates (cred-analysis turns
the scraper's timestamps into local YYYY-MM-DD), so everything date-shaped
here (the date window, "the current month", a scrape's calendar date) uses
the deployment's time zone, from TZ (FINANCE_TZ overrides), default
Asia/Jerusalem. Timestamps the plugin records itself (created_at,
applied_at, expires_at) are UTC ISO-8601 strings with a Z, which sort and
compare correctly as text.

Tests pass a fake `now` to step over the 24-hour abandon window and the
7-day refresh expiry without sleeping.
"""

import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aab_plugin_runtime.logging_setup import kv

log = logging.getLogger(__name__)

DEFAULT_TZ = "Asia/Jerusalem"


def zone(name: str | None) -> ZoneInfo:
    """The named zone, or the default when the name is empty or unknown (a
    wrong TZ only shifts day boundaries; it must not stop the plugin)."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("time zone not recognised; using the default %s",
                        kv(default=DEFAULT_TZ))
    return ZoneInfo(DEFAULT_TZ)


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, tz: str | None = DEFAULT_TZ, now: Callable[[], float] = time.time):
        self.tz = zone(tz)
        self._now = now

    def now(self) -> float:
        return self._now()

    def iso_now(self, offset_seconds: float = 0) -> str:
        return iso_utc(self._now() + offset_seconds)

    def today(self) -> date:
        return datetime.fromtimestamp(self._now(), self.tz).date()

    def current_month(self) -> str:
        return self.today().strftime("%Y-%m")

    def days_ago(self, days: int) -> str:
        """The cutoff date `days` before today. A window reaching past the
        calendar's start (date_window_days has no maximum) is all history,
        not an overflow."""
        try:
            return (self.today() - timedelta(days=days)).isoformat()
        except OverflowError:
            return date.min.isoformat()

    def local_date(self, iso: str) -> str:
        """The local calendar date of an ISO timestamp (a scrape's
        `scraped_at`); today when it is empty or unreadable."""
        if iso:
            try:
                parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    return parsed.date().isoformat()
                return parsed.astimezone(self.tz).date().isoformat()
        return self.today().isoformat()
