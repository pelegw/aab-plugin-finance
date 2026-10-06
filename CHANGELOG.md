# Changelog

All notable changes to this project are documented here. The version number
lives only in `VERSION`.

## [0.1.0] - unreleased (tagged at install, plan phase 2)

### Added
- The finance plugin service (`aab_plugin_finance`), manifest `finance`
  0.1.0: 16 reads (`snapshot_info`, `list_sources`, `list_transactions`,
  `get_transaction`, `search_transactions`, `monthly_summary`,
  `by_category`, `top_merchants`, `recurring_merchants`, `subscriptions`,
  `installments`, `foreign_currency`, `largest_purchases`, `list_notes`,
  `list_runs`, `list_refresh_requests`) and 4 writes (`set_note`
  direct/draft, `request_refresh` draft only, `ingest_snapshot` and
  `report_refresh` direct only), `connection.kind: none`, nothing to
  configure.
- Chunked, idempotent `ingest_snapshot` (sha256 per chunk, any order,
  duplicate/409/replay-after-apply, 24-hour abandon sweep) with the
  cred-analysis `mergeRows` semantics in one transaction.
- Aggregates ported from cred-analysis `analysis.js`, cross-checked against
  it under Node when available.
- Narrowings `card`, `account` (lists) and `company` (uploads); constraints
  `date_window_days`, `detail`, `notes`, `merchant_names`.
- Refresh requests: owner-approved drafts, claimed once, completed or failed,
  expiring after 7 days.
- `aab-plugin.yaml` (descriptor schema 1) and a Dockerfile on
  `ghcr.io/pelegw/aab-plugin-base:0.3.0`.
- `tools/replay_raw.py`: first bulk load from cred-analysis raw snapshots.
- Unit tests (runtime only) and an integration suite inside the gateway's
  broker; CI gated on the `GATEWAY_TOKEN` secret.

### Differs from the approved plan
- `transaction` resource has `resolve: false`: the broker's agent-facing
  resolve filters by the transaction kind only and would list transactions of
  hidden cards.
- The `card` and `account` narrowings also apply to `snapshot_info` and
  `list_runs` (both return per-card rows), and `merchant_names` also applies
  to `list_notes` (it returns transactions with descriptions).
- `ingest_runs.source_kind` (a column the plan's schema lacks): a staging run
  has no `sources` row yet, and `list_runs` must apply that kind's visibility.
- The runtime dependency is a version range plus a `runtime` extra holding the
  git URL, instead of a direct-URL dependency (see README "Development").
