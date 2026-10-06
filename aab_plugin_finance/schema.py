"""The finance database schema, created at boot, changed only additively.

The plugin is the single source of record for the owner's transactions. Its
schema therefore follows the gateway's rule for broker.db:
  * New tables go in SCHEMA.
  * New columns go in MIGRATIONS.
  * No change ever renames or drops anything.
`apply()` is idempotent and runs on every boot. If the volume was not ready at
boot, it runs lazily on first use.

Amounts are integer hundredths (`*_x100`). Sums are exact, and no float ever
needs rounding on the way in. The sign convention is the same as in
cred-analysis: negative = purchase, positive = refund or credit.

This schema has one column that the approved plan's schema does not have:
`ingest_runs.source_kind`. A run that is still staging has no `sources` row
yet. `list_runs` must know the kind of a run (card or account) to apply that
kind's visibility to it.
"""

import sqlite3

SCHEMA_VERSION = 1

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT)""",
    """CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('card', 'account')),
        company TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        last4 TEXT NOT NULL DEFAULT '',
        currency TEXT NOT NULL DEFAULT 'ILS',
        balance_x100 INTEGER,
        balance_at TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        last_run_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        source_id TEXT NOT NULL REFERENCES sources(id),
        source_kind TEXT NOT NULL,
        company TEXT NOT NULL,
        date TEXT NOT NULL,
        processed_date TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL,
        merchant_key TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        original_x100 INTEGER NOT NULL,
        original_currency TEXT NOT NULL,
        charged_x100 INTEGER NOT NULL,
        charged_currency TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'normal',
        installment_number INTEGER,
        installment_total INTEGER,
        status TEXT NOT NULL CHECK (status IN ('completed', 'pending')),
        identifier TEXT NOT NULL DEFAULT '',
        memo TEXT NOT NULL DEFAULT '',
        is_transfer INTEGER NOT NULL DEFAULT 0,
        is_foreign INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        last_seen_run_id TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS tx_source_date ON transactions(source_id, date)",
    "CREATE INDEX IF NOT EXISTS tx_date ON transactions(date)",
    "CREATE INDEX IF NOT EXISTS tx_merchant ON transactions(merchant_key)",
    """CREATE TABLE IF NOT EXISTS notes (
        key TEXT PRIMARY KEY REFERENCES transactions(key) ON DELETE CASCADE,
        note TEXT NOT NULL,
        author TEXT NOT NULL DEFAULT 'agent',
        updated_at TEXT NOT NULL,
        request_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS note_history (
        id INTEGER PRIMARY KEY,
        key TEXT,
        note TEXT,
        author TEXT,
        created_at TEXT,
        request_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS ingest_runs (
        run_id TEXT PRIMARY KEY,
        company TEXT,
        source_id TEXT,
        source_kind TEXT,
        range_start TEXT,
        range_end TEXT,
        chunks_total INTEGER,
        status TEXT CHECK (status IN ('staging', 'applied', 'abandoned')),
        refresh_id TEXT,
        scraped_at TEXT,
        created_at TEXT,
        applied_at TEXT,
        added INTEGER,
        updated INTEGER,
        unchanged INTEGER,
        removed_pending INTEGER)""",
    """CREATE TABLE IF NOT EXISTS ingest_chunks (
        run_id TEXT,
        idx INTEGER,
        sha256 TEXT NOT NULL,
        payload TEXT NOT NULL,
        received_at TEXT,
        PRIMARY KEY (run_id, idx))""",
    """CREATE TABLE IF NOT EXISTS refresh_requests (
        id TEXT PRIMARY KEY,
        company TEXT,
        range_start TEXT,
        range_end TEXT,
        reason TEXT,
        status TEXT CHECK (status IN ('approved', 'running', 'completed', 'failed', 'expired')),
        created_at TEXT,
        expires_at TEXT,
        request_id TEXT,
        claimed_at TEXT,
        finished_at TEXT,
        message TEXT NOT NULL DEFAULT '',
        run_ids TEXT NOT NULL DEFAULT '[]')""",
)

# (table, column, column definition) to add to an existing database. It is
# empty at version 1. Append each later column here. Never add a column only
# to SCHEMA.
MIGRATIONS: tuple[tuple[str, str, str], ...] = ()


def apply(conn: sqlite3.Connection,
          migrations: tuple[tuple[str, str, str], ...] = MIGRATIONS) -> None:
    """Create every table and add every missing column. Idempotent."""
    for statement in SCHEMA:
        conn.execute(statement)
    for table, column, definition in migrations:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (str(SCHEMA_VERSION),))
