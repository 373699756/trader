# Recover the three-strategy recommendation funnel and expose its true first blocker

## User Request

Repair the system-wide failures found in the Today, Tomorrow, and D25 recommendation chain; make the existing
plugin/agent diagnostic path useful when the chain fails; and determine whether the `today-v3`, `tomorrow-v3`, and
`d25-v3` data is actually consumed. This batch must not change historical-data downloading or historical-training
logic.

Regression-Key: `three-strategy-funnel-first-blocker-recovery`

## Cause

Confirmed from the running service before the change:

- Eastmoney `f124` was treated as every stock's actionable full-snapshot time even though it is the stock's last-change
  time. That incorrectly required every stock to trade within the 20/30-second strategy window; 5,089 of 5,291 rows
  became stale while the full response itself was fresh. A second ordering defect could also preserve an older Sina
  cycle over a later completed Eastmoney cycle because the two suppliers express source time differently.
- The primary blocker then reported `strategy_history_unavailable`, hiding the earlier stale-market failure.
- A 5,291-row committed research population exceeded the fixed 4 MiB payload limit. The observer failed open as
  designed, but remained degraded with `ResearchTraceCapacityError` and wrote no audit row.
- The Web displayed only candidate/scored/formal totals, while the typed runtime already owned 24 funnel stages. The
  diagnostic parser monitored only five downstream fields, read neither top-level observer degradation nor scoring-head
  computation, and the unified diagnostic discarded the scoring-head facts emitted by its Web-health child report.

After fixing the false stale classification, a fresh live cycle reached 5,291 rows with only 12 stale rows. That exposed
the next independent break: in a later sample Tomorrow and D25 each had 115 strategy-history-qualified rows but zero
model-input-qualified rows. The active suppliers also continued to miss full-market deadlines, so genuine snapshot age
can still exceed the strict freshness window. This batch makes those states explicit; per the user constraint it does
not change history download, runtime history acquisition, feature construction, training, or model artifacts.

## Added

- Added regression coverage for a later Eastmoney full-market cycle replacing an older cross-source Sina snapshot.
- Added 5,291-row research-audit persistence/restart coverage and a bounded decompression-capacity test.
- Added first-blocker diagnostics for dominant stale quotes, dominant missing liquidity history, and committed-decision
  observer failures.
- Added a typed `model_input_unavailable` first blocker and diagnostic when strategy-history-qualified rows collapse to
  zero at the active model contract.
- Added all 24 existing typed filter/scoring/action nodes to the Web funnel and to the reusable Web-health parser/report.
- Added a bounded scoring-profile projection to both the Web-health report and the unified diagnostic report, exposing
  only profile/model identity plus request, candidate, predictor-batch, and cache-hit counts.
- Added explicit Web messages for `market_population_stale`, `market_liquidity_history_unavailable`, and
  `model_input_unavailable`.

## Changed

- Changed cross-source preservation between complete Eastmoney and Sina cycles to use acquisition receipt watermarks;
  source time still controls action freshness, and same-source/Tencent ordering remains unchanged.
- Changed Eastmoney full-market normalization to use the completed response's receipt timestamp as its snapshot time and
  stopped requesting `f124`; minute and other event-time feeds are unchanged.
- Changed primary-blocker ordering so a dominant stale population is reported before downstream history/model gates;
  when quotes are fresh but dominant liquidity history is absent, the blocker is now
  `market_liquidity_history_unavailable` instead of the generic `no_scored_candidates`.
- Changed research trace persistence to use a self-identifying zlib representation when smaller. Content hashes and
  idempotency remain over canonical decoded JSON, legacy raw rows remain readable, the persisted payload limit remains
  4 MiB, and decoded content is capped at 64 MiB.
- Changed the Web summary to prefer current-day runtime input quality over a retained recommendation snapshot, so a
  current broken stage cannot be hidden by old coverage counts.
- Updated the scoring and engineering authorities for cross-source chronology, first-blocker precedence, audit
  representation, diagnostic projections, and the explicit no-history-I/O behavior of diagnostics.

## Fixed

- A successfully completed new full-market cycle can no longer be silently replaced row-by-row by the preceding
  provider cycle solely because the providers express source timestamps differently.
- Stocks that simply have not traded in the last 20/30 seconds are no longer falsely rejected as stale when their
  Eastmoney full-market batch is current.
- Large but bounded full-market research audits no longer fail only because their canonical JSON exceeds 4 MiB.
- The supported diagnostic entrypoint now explains the first broken stage and distinguishes “model loaded but no row
  reached inference” from “model not loaded”.

## Removed

- Removed no data, history, recommendation records, models, runtime databases, or training artifacts.
- No history download, history acquisition, feature-training, trainer, sample-generation, or model-artifact code was
  modified.

## V3 Artifact Consumption

- The default runtime profile is V2, but its three active heads load the existing shared flat artifacts from
  `data/train/today-v3`, `data/train/tomorrow-v3`, and `data/train/d25-v3`.
- The running service reported matching model hashes:
  Today `940d251d...`, Tomorrow `a4f71a53...`, and D25 `83d49d93...`.
- The duplicate staged paths under `data/train/v3/{today,tomorrow,d25}` are not selected by the current bundle locator.
- In the final live sample all three heads were active. Tomorrow and D25 had one scoring request each; all three still
  had `candidate_count=0` and `predictor_batch_count=0`. The bundles are loaded, but no stock has reached model inference
  because the runtime currently collapses from the strategy-history node to the model-input node.

## Verification

- Focused market gateway, provider, application input/projection, research trace, Web diagnostic, unified diagnostic,
  bootstrap, packaging, and point-in-time population tests passed (191 tests); the dashboard JavaScript state contract
  passed. The diagnostic test proves that a persistent strategy-history-to-model-input collapse is evaluated against
  its direct upstream node instead of an already-empty downstream cache.
- The running service was stopped normally, restarted from `./run.sh`, and served the changed dashboard/API on
  `127.0.0.1:5000`.
- Live sampling observed a fresh 5,291-row cycle with 12 stale rows, proving that per-stock `f124` no longer ages the
  complete batch. Later samples truthfully reported supplier deadlines/circuit opening rather than relabelling old data
  as fresh.
- The final live status exposed all 24 funnel nodes. Tomorrow and D25 each showed 5,279 population, 116 dynamic-filter
  eligible, 115 strategy-history eligible, zero model-input eligible, and `model_input_unavailable`.
- The unified runtime report preserved all three active model identities/hashes and their computation counters without
  exposing stock identities or feature payloads.
- The isolated Firefox refresh gate passed with 18 DOM samples, 1.03-second maximum interval, 10 ms P95
  patch-to-paint, and a successful decision patch.
- Desktop browser acceptance directly asserted the complete 24-node funnel and passed at 1280x720, 1440x900, and
  1920x1080 with no page-level horizontal overflow or browser errors.
- `make format-check`, `make lint`, `make type-check`, and `make package` passed against the complete diff.
- `make test` completed collection/execution but did not pass: eight pre-existing documentation-contract assertions
  require phrases already absent from `HEAD` (the minute-library/authorization plan wording, two scoring-document
  statements, and the obsolete shared-head matrix row). This batch did not edit the affected implementation-plan
  documents or restore stale V2/training wording, because doing so would violate the requested history/training scope.

## Residual Risks

- The three strategies still cannot score the current live population. Most rows lack runtime liquidity inputs, and the
  rows that pass the strategy-history stage currently fail the active model input contract. Correcting source taxonomy or
  feature evidence requires a separately authorized change because this batch may not alter historical acquisition or
  training; no industry mapping or feature value was fabricated.
- External full-market, candidate, news, risk, reference-data, and runtime-history suppliers continue to show intermittent
  timeouts or unavailable states. The service remains read-only and fail-open, but recommendation readiness depends on
  those inputs.
- The large-audit behavior is proven with the production codec and SQLite archive using a representative 5,291-row
  event. A live observer write did not occur because no current candidate reached a committed scored decision.
