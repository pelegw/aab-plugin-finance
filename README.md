# aab-plugin-finance

The finance plugin for the [Agent Authority Broker](https://github.com/pelegw/agent-authority-broker)
(the gateway): the owner's credit card (and later bank account) transactions,
uploaded by the owner's own scraper, read and annotated by AI agents strictly
inside the grants the broker hands this service with every call.

It is an **external plugin package**: its own repository, installed into a
running gateway from the console (Plugins, + Add plugin,
`github.com/pelegw/aab-plugin-finance@v0.1.0`), never vendored into the gateway.
The owner reviews and pins the manifest at install time; nothing here can
widen it afterwards.

- Design (source of truth for the data model, ingest contract, manifest,
  modules, security and tests): `C:\Users\Peleg\Documents\finance-plugin-plan.md`
  sections 3.1-3.6 and 6.
- Packaging and installation (this repository's shape, the descriptor, the
  base image): `C:\Users\Peleg\.claude\plans\majestic-conjuring-dove.md`
  ("Design" and "Phase 1").

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

Amounts travel and are stored as integer hundredths (`*_x100`); results show
units (`charged_amount: -123.45`; negative = purchase, positive = refund).

Narrowing is capability data in the broker: `selector: {card: [...]}` or
`{account: [...]}`, and the constraints `date_window_days`, `detail`
(`aggregate` = totals only), `notes` and `merchant_names` (false = opaque
merchant ids). The two keys the plan starts with (section 2):

```json
{"name": "mac-mini-scraper", "role": "read-act", "capabilities": [
  {"target": "finance", "actions": ["ingest_snapshot", "report_refresh", "list_refresh_requests", "list_runs"],
   "mode": "direct", "budget": {"per_day": 2000}}]}
{"name": "analysis-agent", "role": "read-act", "capabilities": [
  {"target": "finance", "actions": ["read_*", "set_note"], "mode": "direct"},
  {"target": "finance", "actions": ["request_refresh"], "mode": "draft", "budget": {"per_day": 3}}]}
```

## Security properties

- Visibility is applied inside every SQL query, aggregates included: a hidden
  card, account or transaction (owner-hidden or a key's own deny), a card
  outside the capability's selector (`allow_only`, where `[]` means none), a
  denied company and rows outside the date window are never read, so they can
  neither appear in a list nor move a total. Hidden == missing: one 404.
- Scope parsing fails closed: a malformed scope or an undeclared constraint
  is a 400, never "unrestricted".
- `ingest_snapshot` is bound to the `company` resource: hiding a card does not
  stop its uploads (the record stays complete) while every read of it stays
  404. A compromised scraper key can write rows and handle refresh requests,
  but cannot read transactions.
- No credential exists in this plugin (`connection.kind: none`,
  `config_schema: []`; `/secrets` stays empty). The database path is
  container env, not configurable over the network.
- Logs carry actions, statuses, run ids, chunk indexes, row counts and
  refresh ids; never descriptions, amounts, keys, tx ids, notes or source
  ids. `tests/test_logging.py` sweeps DEBUG output for planted values.
- Writes are single `BEGIN IMMEDIATE` transactions: anything failing before
  COMMIT is rolled back and answered 503 (not performed, safe to retry).

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
(`/data/finance.db`, SQLite, WAL). Back it up like the gateway's other data
volumes: it is the source of record.

## Development

The runtime (`aab-plugin-runtime`) and, for some tests, the broker come from
a gateway checkout. `AAB_SRC` names it; the default is the sibling directory
`../agent-authority-broker`.

```bash
python -m venv .venv            # Python 3.12+
AAB_SRC=${AAB_SRC:-../agent-authority-broker}
.venv/Scripts/pip install -e "$AAB_SRC/plugin-runtime" -e ".[dev]"   # bin/ on Linux and macOS
.venv/Scripts/pip install -e "$AAB_SRC/broker"                       # tests only
```

Why the runtime is not pulled from `pyproject.toml` here: it is declared as
`aab-plugin-runtime>=0.2.0,<1`, satisfied by the editable checkout above and
by the base image in the container. The git URL at the gateway tag is the
`runtime` extra (`pip install ".[runtime]"`) for a standalone install. A
direct-URL dependency would be re-resolved by pip even with a runtime
already installed (conflicting with the checkout, and making the Docker
build clone a private repository).

```bash
.venv/Scripts/python -m pytest --ignore=tests/integration      # unit
AAB_SRC=../agent-authority-broker .venv/Scripts/python -m pytest tests/integration
.venv/Scripts/python -m pytest                                  # both
```

The integration tests are skipped when no gateway checkout is found
(`AAB_REQUIRE_GATEWAY=1` turns that into an error, as in CI). They register
the adapter in the gateway's registry through its test seam
(`Registry(vendored_dirs=...)` with this manifest standing in for the owner's
pin), in process and over the plugin API, and run the plan's key A / key B
scenarios, hidden-card 404s, the draft refresh approval and the MCP tool list.

Two optional cross-checks run when Node and a cred-analysis checkout are
present (`CRED_ANALYSIS_SRC`, default `../cred-analysis`): the aggregates
against the real `analysis.js`, and the replay tool's row keys against the
real `normalize.js`.

## First bulk load: tools/replay_raw.py

Uploads cred-analysis's raw snapshots through the broker with key A, in
`scrapedAt` order, in chunks of 400, with the same row keys the scraper's
normalizer produces (so later uploads merge with them). Standard library
only; prints run ids and counts, never transactions or the key.

```bash
python tools/replay_raw.py --raw ../cred-analysis/data/raw --dry-run
AAB_KEY=aab_... python tools/replay_raw.py --raw ../cred-analysis/data/raw --url https://aab.example.com
```

Dates are converted in this machine's local time zone, exactly as
cred-analysis did when it built the workbook (`--tz` overrides; on Windows
that needs `pip install tzdata`). Retries 503/429 with backoff, resends on
502/504 (the chunk hash makes that idempotent), stops on any other error.
Replayed into a local database, the owner's raw snapshots give the same row
count and the same yearly net total as the owner's workbook.

## CI

`.github/workflows/ci.yml` runs the unit and integration suites against a
gateway checkout, and builds the image against the base image. Both need the
repository secret **`GATEWAY_TOKEN`**: a classic personal access token with
`repo` (to check out the private gateway) and `read:packages` (to pull the
private base image from GHCR). Without it the jobs pass but say, in a warning
annotation and the job summary, that nothing ran; CI is meaningful only once
the secret exists. The gateway ref defaults to the runtime tag `v0.3.0`; set
the repository variable `GATEWAY_REF` (for example `dev`) to test against
another ref before that tag is published.

## Versions

The package version lives only in `VERSION`. The manifest's `version` moves
with every change to actions, narrowings or constraints (the owner re-pins
on upgrade).
