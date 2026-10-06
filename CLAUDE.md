# CLAUDE.md

Guidance for anyone (human or AI) who works in this repository.

## What this is

This is the finance plugin for the Agent Authority Broker (the gateway,
pelegw/agent-authority-broker). The owner's scraper uploads card transactions,
and later bank transactions. Agents read, aggregate and annotate them, strictly
inside the grant that the broker sends with every call.

This is a standalone plugin package. The gateway's installer builds it from
`aab-plugin.yaml` and `Dockerfile`. No commit to the gateway repository ever
contains any part of it.

- Plugin design: `C:\Users\Peleg\Documents\finance-plugin-plan.md`. It has the
  data model (3.1), the ingest contract (3.2), the manifest (3.3), the modules
  (3.4), security (3.6) and tests (6). The next file replaces its packaging
  parts: the vendored copy in 3.3, section 3.5, and the gateway-side items of
  6 and 7.
- Packaging, installer, phases: `C:\Users\Peleg\.claude\plans\majestic-conjuring-dove.md`.

## Run the tests

```bash
AAB_SRC=${AAB_SRC:-../agent-authority-broker}
python -m venv .venv && .venv/Scripts/pip install -e "$AAB_SRC/plugin-runtime" -e ".[dev]"
.venv/Scripts/pip install -e "$AAB_SRC/broker"      # tests only (manifest check, integration)
.venv/Scripts/python -m pytest                      # unit + integration (skipped without a gateway)
```

Make sure that the tests pass before every commit. CI and the server build the
Docker image. By default, you do not build it locally.

## Conventions

**Code shape**
- Write small, focused modules. Every module starts with a docstring that says
  what the module is and why it exists. Comments explain *why*, for a human
  reader.
- Write a test for every behaviour (pytest and `fastapi.testclient`). Every bug
  fix comes with a regression test. Most tests go through the real plugin
  runtime (`tests/conftest.py`: `perform(...)`, `scope(...)`). Tests seed data
  only through `ingest_snapshot` (`tests/fakes.py`). They never write to the
  database directly.
- Copy patterns from the gateway: the adapter shape, fail-closed scope parsing,
  `_visibility_clause`. Never import them from the gateway. The plugin depends
  only on `aab-plugin-runtime`. Only the tests import the broker.
- Storage is raw `sqlite3` (`store.py`, `schema.py`). Schema changes are
  additive only. New tables go in `SCHEMA`, and new columns go in `MIGRATIONS`.
  Never rename or drop a column. The database is the owner's source of record.
- Inside the plugin, amounts are always integer hundredths. Floats occur only
  in results.
- Run one uvicorn worker, so that there is one writer. Each write is one
  `BEGIN IMMEDIATE` database transaction. Any failure before COMMIT is a 503.
- The aggregates are a verbatim port of cred-analysis `src/analysis.js`: the
  heuristics, the thresholds, the JavaScript regex and the whitespace
  semantics. Change them only together with a test that states the new rule.

**Authority rules (never bend these)**
- Apply visibility inside every query (store.`tx_clause`), aggregates
  included. `allow_only: []` sees nothing. A malformed scope or an unknown
  constraint is a 400.
- Hidden == 404, with one message, for cards, accounts and transactions. Every
  list and every total excludes a hidden id.
- Every row that names a card or an account carries `resource_ref`.
- The manifest is the contract. If an action has no handler, or a handler has
  no action, the plugin does not boot. Bump `version` in `manifest.yaml` on
  every change to actions, narrowings or constraints.
- `request_refresh` stays `modes: [draft]`, so a human always approves it.
  `ingest_snapshot` and `report_refresh` stay `modes: [direct]`.
- Name each flag for the permission that it grants (`true` = permissive).

**Logging** (the gateway's `docs/logging.md`)
- Use `log = logging.getLogger(__name__)`. Write a fixed message plus `kv(...)`
  for the variable part. For a write, log after COMMIT.
- Permitted in logs: action, status, run id, chunk index, row counts, refresh
  ids, exception type names.
- Never in logs: descriptions, memos, categories, amounts, row keys, tx ids,
  source ids, notes, refresh reasons or messages, params, results, secrets or
  tokens. `tests/test_logging.py` sweeps the DEBUG output.
- Error messages name the field or the rule. They never contain the caller's
  value.

**Packaging**
- The version lives only in `VERSION`. Hatch reads it from there.
- `aab-plugin.yaml` follows the strict schema 1. The installer rejects extra
  keys. `tests/test_packaging.py` holds this file, the Dockerfile and
  pyproject to the contracts of the installer and the base image.
- The runtime dependency in `dependencies` is a version range, never a direct
  URL (see README "Development"). The git URL is in the `runtime` extra.

## Git

Develop on `dev`. Merge to `main` only when the owner asks. Commit messages end
with the session's attribution trailer.
