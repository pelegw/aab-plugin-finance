# Changelog

This file records all notable changes to this project. The version number
lives only in `VERSION`.

## [0.1.0] - unreleased (tagged at install, plan phase 2)

### Added
- The finance plugin service (`aab_plugin_finance`), with manifest `finance`
  0.1.0. It has `connection.kind: none` and nothing to configure. It has these
  actions:
  - 16 reads: `snapshot_info`, `list_sources`, `list_transactions`,
    `get_transaction`, `search_transactions`, `monthly_summary`,
    `by_category`, `top_merchants`, `recurring_merchants`, `subscriptions`,
    `installments`, `foreign_currency`, `largest_purchases`, `list_notes`,
    `list_runs`, `list_refresh_requests`.
  - 4 writes: `set_note` (direct or draft), `request_refresh` (draft only),
    `ingest_snapshot` and `report_refresh` (direct only).
- A chunked, idempotent `ingest_snapshot`. It keeps a sha256 per chunk and
  accepts chunks in any order. It answers duplicate, 409 and
  replay-after-apply, and it sweeps abandoned runs after 24 hours. It applies
  the cred-analysis `mergeRows` semantics in one database transaction.
- Aggregates ported from cred-analysis `analysis.js`. When Node is available,
  a test cross-checks them against `analysis.js`.
- The narrowings `card` and `account` (lists) and `company` (uploads).
- The constraints `date_window_days`, `detail`, `notes` and `merchant_names`.
- Refresh requests. Each one is a draft that the owner approves. The scraper
  claims it once and marks it completed or failed. It expires after 7 days.
- `aab-plugin.yaml` (descriptor schema 1) and a Dockerfile on
  `ghcr.io/pelegw/aab-plugin-base:0.3.0`.
- `tools/replay_raw.py`: the first bulk load from the raw snapshots of
  cred-analysis.
- Unit tests (runtime only) and an integration suite inside the gateway's
  broker.
- CI, gated on the `GATEWAY_TOKEN` secret.

### Differs from the approved plan
- The `transaction` resource has `resolve: false`. The broker's agent-facing
  resolve filters by the transaction kind only. It would list transactions of
  hidden cards.
- The `card` and `account` narrowings also apply to `snapshot_info` and
  `list_runs`. Both actions return per-card rows.
- `merchant_names` also applies to `list_notes`, because that action returns
  transactions with descriptions.
- `ingest_runs.source_kind`, a column that the plan's schema does not have. A
  staging run has no `sources` row yet, and `list_runs` must apply the
  visibility of that kind.
- The runtime dependency is a version range, plus a `runtime` extra that holds
  the git URL. The plan had a direct-URL dependency (see README
  "Development").
