"""The plugin's notion of "now" and "today", in one injectable object.

Dates in the data are the owner's local calendar dates. cred-analysis turns
the scraper's timestamps into local YYYY-MM-DD. Everything date-shaped here
therefore uses the time zone of the deployment:
  * The date window.
  * "The current month".
  * The calendar date of a scrape.
The zone comes from TZ, and FINANCE_TZ overrides it. The default is
Asia/Jerusalem.

The plugin also records its own timestamps (created_at, applied_at,
expires_at). These are UTC ISO-8601 strings with a Z, which sort and compare
correctly as text.

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
    """The named zone, or the default when the name is empty or unknown. A
    wrong TZ only shifts day boundaries, and it must not stop the plugin."""
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
        """The cutoff date `days` before today. date_window_days has no
        maximum. A window that reaches past the start of the calendar means
        all history, not an overflow."""
        try:
            return (self.today() - timedelta(days=days)).isoformat()
        except OverflowError:
            return date.min.isoformat()

    def local_date(self, iso: str) -> str:
        """The local calendar date of an ISO timestamp, such as a scrape's
        `scraped_at`. It returns today when the timestamp is empty or
        unreadable."""
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
