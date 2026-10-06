# aab-plugin-finance

This is the finance plugin for the
[Agent Authority Broker](https://github.com/pelegw/agent-authority-broker).
This document calls that project the gateway.

The plugin keeps the owner's credit card transactions. Later it will also keep
bank account transactions. The owner's own scraper uploads them. AI agents read
and annotate them, strictly inside the grants that the broker sends to this
service with every call.

It is an **external plugin package**. It has its own repository. The owner
installs it into a running gateway from the console: Plugins, + Add plugin,
`github.com/pelegw/aab-plugin-finance@v0.1.0`. The gateway repository never
contains a copy of it. The owner reviews and pins the manifest at install
time. After that, nothing in this repository can widen the manifest.

- Design: `C:\Users\Peleg\Documents\finance-plugin-plan.md`, sections 3.1-3.6
  and 6. It is the source of truth for the data model, the ingest contract,
  the manifest, the modules, security and tests.
- Packaging and installation: `C:\Users\Peleg\.claude\plans\majestic-conjuring-dove.md`,
  sections "Design" and "Phase 1". It describes the shape of this repository,
  the descriptor and the base image.

## What it does

| Action | Effect, modes | What for |
|---|---|---|
| `snapshot_info` | read | How fresh the data is; agents call it first |
| `list_sources` | read | Cards and accounts with counts, date span, balance, last upload |
| `list_transactions` | read | Newest first, keyset paging; filters: source, dates or month, status, kind, minimum amount |
| `get_transaction` | read (resource `transaction`) | One row by its `tx_` id |
| `search_transactions` | read | Substring search over descriptions, memos, categories, notes |
| `monthly_summary` | read | Month x card or category: count, spend, refunds, net, change; totals |
| `by_category` | read | Net and share per category |
| `top_merchants`, `recurring_merchants`, `subscriptions` | read | The cred-analysis `analysis.js` heuristics |
| `installments` | read | Plans: paid, remaining, monthly commitment |
| `foreign_currency` | read | Per currency, with the implied exchange rate |
| `largest_purchases` | read | The largest single purchases |
| `list_notes` | read | Agent notes with their transactions |
| `list_runs` | read | Upload runs (no transaction content) |
| `list_refresh_requests` | read | Approved refresh requests and how they ended |
| `set_note` | write, `[direct, draft]` | Write or clear the note on a transaction |
| `request_refresh` | write, `[draft]` | Ask for fresh data; **always** an approval card for the owner |
| `ingest_snapshot` | write, `[direct]` (resource `company`) | The scraper's chunked, idempotent upload |
| `report_refresh` | write, `[direct]` | The scraper claims and finishes a refresh request |

Uploads and the database use integer hundredths for amounts (`*_x100`).
Results show units, for example `charged_amount: -123.45`. A negative amount
is a purchase. A positive amount is a refund.

The broker keeps each narrowing as capability data. A capability can narrow a
key in these ways:

- A selector: `selector: {card: [...]}` or `{account: [...]}`.
- The constraint `date_window_days`.
- The constraint `detail`. The value `aggregate` permits totals only.
- The constraint `notes`.
- The constraint `merchant_names`. The value `false` replaces merchant names
  with opaque merchant ids.

The plan starts with these two keys (section 2).

```json
{"name": "mac-mini-scraper", "role": "read-act", "capabilities": [
  {"target": "finance", "actions": ["ingest_snapshot", "report_refresh", "list_refresh_requests", "list_runs"],
   "mode": "direct", "budget": {"per_day": 2000}}]}
{"name": "analysis-agent", "role": "read-act", "capabilities": [
  {"target": "finance", "actions": ["read_*", "set_note"], "mode": "direct"},
  {"target": "finance", "actions": ["request_refresh"], "mode": "draft", "budget": {"per_day": 3}}]}
```

## Security properties

- The plugin applies visibility inside every SQL query, aggregates included.
  The queries never read the rows below, so these rows cannot appear in a list
  or move a total:
  - A hidden card, account or transaction. The owner hid it, or the key's own
    grant denies it.
  - A card outside the capability's selector (`allow_only`). The empty list
    `[]` means no card.
  - A denied company.
  - Rows outside the date window.

  Hidden == missing: the plugin gives the same 404 for both.
- Scope parsing fails closed. A malformed scope or an undeclared constraint
  gets a 400. It never means "unrestricted".
- In the manifest, the resource of `ingest_snapshot` is `company`. When the
  owner hides a card, uploads for that card continue, so the record stays
  complete. Every read of the hidden card still gets a 404. A compromised
  scraper key can write rows and handle refresh requests, but it cannot read
  transactions.
- This plugin holds no credential: `connection.kind: none` and
  `config_schema: []`, and `/secrets` stays empty. The database path comes
  from the container environment. No call over the network can change it.
- Logs carry actions, statuses, run ids, chunk indexes, row counts and refresh
  ids. Logs never carry descriptions, amounts, row keys, tx ids, notes or
  source ids. `tests/test_logging.py` sweeps the DEBUG output for planted
  values.
- Each write is one `BEGIN IMMEDIATE` database transaction. If anything fails
  before COMMIT, the plugin rolls the write back and answers 503. A 503 means
  that the plugin did nothing, and a retry is safe.

## Layout

```
aab-plugin.yaml            the package descriptor the gateway's installer reads (schema 1)
Dockerfile                 FROM ghcr.io/pelegw/aab-plugin-base:0.3.0
aab_plugin_finance/        the adapter (manifest.yaml, adapter.py, store.py, ingest.py, ...)
tests/                     unit tests (plugin runtime only)
tests/integration/         the plugin inside the gateway's broker (needs a gateway checkout)
tools/replay_raw.py        first bulk load from cred-analysis data/raw/*.json
```

The data lives in the container's own `finance_data` volume
(`/data/finance.db`, SQLite, WAL). This database is the source of record.
Back it up like the other data volumes of the gateway.

## Development

The runtime (`aab-plugin-runtime`) comes from a gateway checkout. Some tests
also need the broker from that checkout. `AAB_SRC` names the checkout. The
default is the sibling directory `../agent-authority-broker`.

```bash
python -m venv .venv            # Python 3.12+
AAB_SRC=${AAB_SRC:-../agent-authority-broker}
.venv/Scripts/pip install -e "$AAB_SRC/plugin-runtime" -e ".[dev]"   # bin/ on Linux and macOS
.venv/Scripts/pip install -e "$AAB_SRC/broker"                       # tests only
```

The steps above do not install the runtime from `pyproject.toml`, for these
reasons:

- `pyproject.toml` declares the runtime as `aab-plugin-runtime>=0.2.0,<1`.
  The editable checkout above satisfies that range. The base image in the
  container satisfies it too.
- For a standalone install, the `runtime` extra holds the git URL at the
  gateway tag: `pip install ".[runtime]"`.
- A direct-URL dependency would make pip resolve the URL again, even with a
  runtime already installed. That would conflict with the checkout. It would
  also make the Docker build clone a private repository.

```bash
.venv/Scripts/python -m pytest --ignore=tests/integration      # unit
AAB_SRC=../agent-authority-broker .venv/Scripts/python -m pytest tests/integration
.venv/Scripts/python -m pytest                                  # both
```

Without a gateway checkout, pytest skips the integration tests.
`AAB_REQUIRE_GATEWAY=1` makes a missing checkout an error, as in CI. The
integration tests register the adapter in the gateway's registry through its
test seam, `Registry(vendored_dirs=...)`. In that seam, this manifest takes
the place of the owner's pin. The tests register the adapter in process and
over the plugin API. Then they run these scenarios from the plan:

- The key A and key B scenarios.
- The 404 for a hidden card.
- The approval of a draft refresh request.
- The MCP tool list.

Two optional cross-checks run when Node and a cred-analysis checkout are
present. `CRED_ANALYSIS_SRC` names the checkout. The default is
`../cred-analysis`. The cross-checks are:

- The aggregates against the real `analysis.js`.
- The row keys of the replay tool against the real `normalize.js`.

## First bulk load: tools/replay_raw.py

This tool uploads the raw snapshots of cred-analysis through the broker with
key A. It sends the snapshots in `scrapedAt` order, in chunks of 400 rows. It
uses the same row keys that the scraper's normalizer makes, so later uploads
merge with these rows. The tool uses only the standard library. It prints run
ids and counts. It never prints transactions or the key.

```bash
python tools/replay_raw.py --raw ../cred-analysis/data/raw --dry-run
AAB_KEY=aab_... python tools/replay_raw.py --raw ../cred-analysis/data/raw --url https://aab.example.com
```

The tool converts dates in the local time zone of this machine, exactly as
cred-analysis did when it built the workbook. `--tz` sets another zone. On
Windows, `--tz` needs `pip install tzdata`. The tool handles errors as
follows:

- On 503 or 429, it retries with backoff.
- On 502 or 504, it sends the chunk again. The chunk hash makes that
  idempotent.
- On any other error, it stops.

A replay of the owner's raw snapshots into a local database gives the same
row count and the same yearly net total as the owner's workbook.

## CI

`.github/workflows/ci.yml` runs the unit and integration suites against a
gateway checkout. It also builds the image against the base image. Both jobs
need the repository secret **`GATEWAY_TOKEN`**. This secret is a classic
personal access token with these permissions:

- `repo`, to check out the private gateway.
- `read:packages`, to pull the private base image from GHCR.

Without the secret, the jobs pass. A warning annotation and the job summary
then say that nothing ran. CI has meaning only when the secret exists. The
default gateway ref is the runtime tag `v0.3.0`. To test against another ref
before the gateway publishes that tag, set the repository variable
`GATEWAY_REF`, for example to `dev`.

## Versions

The package version lives only in `VERSION`. The manifest has its own
`version`. It changes with every change to actions, narrowings or
constraints. On an upgrade, the owner pins the manifest again.

## License

Copyright (C) 2026 Peleg Wasserman.

This program is free software. You can redistribute it and modify it under
the GNU Affero General Public License, version 3 or any later version. See
[LICENSE](LICENSE) for the full text. If you run a modified version as a
service, you must offer its source to the users of that service. A
commercial license for uses the AGPL does not fit is available from the
author.
