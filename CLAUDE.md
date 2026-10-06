# CLAUDE.md

Guidance for anyone (human or AI) working in this repository.

## What this is

The finance plugin for the Agent Authority Broker (the gateway,
pelegw/agent-authority-broker): card (later bank) transactions uploaded by
the owner's scraper, read, aggregated and annotated by agents strictly inside
the grant the broker sends with every call. A standalone plugin package: the
gateway's installer builds it from `aab-plugin.yaml` + `Dockerfile`; nothing
of it is ever committed to the gateway repository.

- Plugin design (data model 3.1, ingest contract 3.2, manifest 3.3, modules
  3.4, security 3.6, tests 6): `C:\Users\Peleg\Documents\finance-plugin-plan.md`.
  Its packaging parts (3.3's vendored copy, 3.5, the gateway-side items of 6
  and 7) are superseded by the next file.
- Packaging, installer, phases: `C:\Users\Peleg\.claude\plans\majestic-conjuring-dove.md`.

## Running tests

```bash
AAB_SRC=${AAB_SRC:-../agent-authority-broker}
python -m venv .venv && .venv/Scripts/pip install -e "$AAB_SRC/plugin-runtime" -e ".[dev]"
.venv/Scripts/pip install -e "$AAB_SRC/broker"      # tests only (manifest check, integration)
.venv/Scripts/python -m pytest                      # unit + integration (skipped without a gateway)
```

Green before every commit. Docker builds happen in CI and on the server, not
locally by default.

## Conventions

**Code shape**
- Small, focused modules. Every module starts with a docstring saying what it
  is and why it exists; comments explain *why*, for a human reader.
- A test for every behaviour (pytest + `fastapi.testclient`); bug fixes come
  with a regression test. Most tests go through the real plugin runtime
  (`tests/conftest.py`: `perform(...)`, `scope(...)`), seeding data only
  through `ingest_snapshot` (`tests/fakes.py`), never by writing the database.
- Patterns are copied from the gateway (adapter shape, fail-closed scope
  parsing, `_visibility_clause`), never imported from it: the plugin depends
  only on `aab-plugin-runtime`. The broker is imported by tests only.
- Storage is raw `sqlite3` (`store.py`, `schema.py`). Schema changes are
  additive only: new tables in `SCHEMA`, new columns in `MIGRATIONS`. Never
  rename or drop a column; the database is the owner's source of record.
- Amounts are integer hundredths everywhere inside; floats only in results.
- One uvicorn worker (one writer). Writes are one `BEGIN IMMEDIATE`
  transaction each; anything failing before COMMIT is a 503.
- The aggregates port cred-analysis `src/analysis.js` verbatim (heuristics,
  thresholds, the JavaScript regex and whitespace semantics). Change them
  only together with a test that states the new rule.

**Authority rules (never bend these)**
- Visibility inside every query (store.`tx_clause`), aggregates included;
  `allow_only: []` sees nothing; malformed scope or unknown constraint = 400.
- Hidden == 404, one message, for cards, accounts and transactions, and a
  hidden id is filtered out of every list and every total.
- Every row naming a card or account carries `resource_ref`.
- The manifest is the contract: an action without a handler (or the reverse)
  refuses to boot. Bump `version` in `manifest.yaml` on every change to
  actions, narrowings or constraints.
- `request_refresh` stays `modes: [draft]` (always a human);
  `ingest_snapshot` and `report_refresh` stay `modes: [direct]`.
- Flags are named for the permission they grant (true = permissive).

**Logging** (the gateway's `docs/logging.md`)
- `log = logging.getLogger(__name__)`, a fixed message plus `kv(...)` for the
  variable part, logged after COMMIT for writes.
- Allowed: action, status, run id, chunk index, row counts, refresh ids,
  exception type names. Never descriptions, memos, categories, amounts, row
  keys, tx ids, source ids, notes, refresh reasons or messages, params,
  results, secrets or tokens. `tests/test_logging.py` sweeps DEBUG output.
- Error messages name the field or rule, never the caller's value.

**Packaging**
- The version lives only in `VERSION` (hatch reads it).
- `aab-plugin.yaml` is strict schema 1 (the installer refuses extra keys);
  `tests/test_packaging.py` holds it, the Dockerfile and pyproject to the
  installer's and the base image's contracts.
- The runtime dependency is a version range, never a direct URL in
  `dependencies` (see README "Development"); the git URL is the `runtime` extra.

## Git

Develop on `dev`; merge to `main` only when asked. Commit messages end with
the session's attribution trailer.
