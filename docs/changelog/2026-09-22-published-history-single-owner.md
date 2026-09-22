# Unify recommendation history with the published download archive

## User request

Explain why the dashboard reduced the full-market population from roughly 5,226 securities to 109 at the data-source/static stage, ensure local history is acquired once at startup instead of repeatedly queried online, and make that path fully compatible with `./run.sh download`.

## Evidence

- The recommendation runtime previously owned a separate 360-entry history cache and an online Tencent/Eastmoney warmup path. That capacity was unrelated to the full eligible market population.
- Static readiness treated missing liquidity history as unavailable input, so most securities were removed before business filtering; the UI then labelled that count as “淘汰”, conflating pending data with a business rejection.
- `./run.sh download` already published a verified BaoStock monthly archive under `data/history/baostock`, but recommendation did not consume its active snapshot.

## Behavior change

- Download and startup maintenance now share `HistorySyncConfiguration.for_repository`, `DownloadHistoryUseCase`, the BaoStock supplier, maintenance lock, checkpoint, and atomic active-snapshot publication.
- Each server process schedules that maintenance once in a non-blocking background thread. Manual download and startup synchronization are therefore compatible and converge on the same archive.
- Recommendation reads the exact active snapshot through a typed download read use case. It builds a replaceable in-memory qfq feature projection and raw/qfq outcome pairs without writing a second historical-feature database.
- Runtime Tencent, Eastmoney, and Tushare history warmup, its dedicated worker pool, repeated persistence, and warmup-triggered rescoring were removed. Reference refresh no longer performs historical requests.
- A missing archive or missing security window is reported as `history_data_pending`; a failed replacement retains the last valid projection. Status exposes snapshot identity, cutoff, coverage, errors, and startup-maintenance progress.
- The dashboard labels non-ready input as “待就绪” rather than “淘汰”.

## Verification

- Archive integration tests create a snapshot with the same synchronizer used by download and verify that recommendation binds its hash, reads 61-session context, retains 20 feature bars, and derives liquidity history.
- Missing-snapshot coverage verifies explicit pending behavior without fabricated history.
- Startup orchestration verifies the shared download use case runs at most once per process instance.
- Full test collection, targeted Ruff, targeted mypy, Web/status/config/persistence/outcome tests, and affected market component tests were run.

## Residual risks

- No production archive was downloaded during this batch, so current local coverage remains unavailable until startup synchronization or `./run.sh download` completes successfully.
- Existing legacy historical-feature tables are ignored but not destructively deleted from activity data.

Regression-Key: `published-history-single-owner-v1`
