"""aab_plugin_finance: the finance plugin service for the Agent Authority Broker.

Runs in its own container (plugin-finance) behind `aab_plugin_runtime`. The
owner's scraper uploads card (later bank) transactions with `ingest_snapshot`;
agents read them, aggregate them, annotate them and ask for fresh data,
each inside the grant the broker hands this service with every call. The
plugin owns its SQLite database (the source of record) and holds no
credential. Design: finance-plugin-plan.md sections 3.1-3.6.
"""

from .adapter import FinanceAdapter
from .main import create_app

__all__ = ["FinanceAdapter", "create_app"]
