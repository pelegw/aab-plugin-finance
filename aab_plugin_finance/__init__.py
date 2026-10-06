"""aab_plugin_finance: the finance plugin service for the Agent Authority Broker.

The plugin runs in its own container (plugin-finance) behind
`aab_plugin_runtime`. The owner's scraper uploads card transactions, and later
bank transactions, with `ingest_snapshot`. Agents read, aggregate and annotate
them, and they ask for fresh data. Each call stays inside the grant that the
broker sends to this service with that call. The plugin owns its SQLite
database (the source of record) and holds no credential. Design:
finance-plugin-plan.md sections 3.1-3.6.
"""

from .adapter import FinanceAdapter
from .main import create_app

__all__ = ["FinanceAdapter", "create_app"]
