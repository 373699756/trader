# Restore industry-aware model inputs and truthful funnel branches

## User Request

Continue the system-wide repair after the Web funnel showed strategy-history-qualified rows collapsing to zero at model
input. Keep the real serial path visible through final selection, separate observation and classification branches, make
the existing diagnostic script identify the real cause, and do not change historical-data download or training logic.

Regression-Key: `current-model-industry-input-recovery`

## Cause

- Confirmed live state before this batch: Tomorrow and D25 repeatedly reached 115 strategy-history-qualified rows but
  zero model-input-qualified rows; all later funnel stages consequently remained zero.
- The three active heads require industry exposure and contain 42 BaoStock/CSRC industry identifiers such as
  `J66货币金融服务`. Eastmoney realtime quotes supplied display-sector names such as `银行` and `软件开发`; the two
  sets had zero exact overlap.
- The model scorer used the quote display industry as if it were the training classification. The existing plugin/agent
  definitions and Web-health script could report the broken stage, but no runtime source supplied the required current
  classification and the script could not identify that missing source.
- The dashboard concatenated serial gates, coverage checks, filter outcomes, review eligibility, action outcomes, and
  final pools into one 24-node arrow chain. It therefore falsely presented parallel branches as more than twenty filters;
  its generic counter formatter also converted an unknown `null` into the numeric value zero.

## Added

- Added a bounded BaoStock current-industry adapter that requests one whole-market snapshot, accepts only exact
  “证监会行业分类” records, runs in a dedicated latest-wins lane, and enforces a 120-second subprocess deadline.
- Added immutable `ModelIndustryReference` evidence to feature snapshots, including industry identifier,
  classification, effective date, source, and data version.
- Added daily refresh, recent-snapshot persistence/recovery, source-health telemetry, and a reusable Web-health finding
  `model_industry_reference_unavailable`.
- Added provider, model-contract, feature-enrichment, recovery, health, and diagnostic regression coverage.

## Changed

- Industry-aware Today, Tomorrow, and D25 heads now consume only the explicit current CSRC model-industry reference.
  Display industry remains unchanged for Web grouping, industry heat, and concentration.
- Native-input and model feature identities now include the model-industry evidence, so a classification change
  invalidates affected cached scoring work deterministically.
- `market_data.sources.baostock_industry` now exposes bounded aggregate source health without stock identities or raw
  payloads.
- The dashboard now renders one formal-selection main line, one observation branch, and separate classification totals.
  Pending downstream work is shown as `—`, not as a confirmed zero.

## Fixed

- A valid strategy-history candidate is no longer rejected merely because Eastmoney and the trained model use different
  industry taxonomies.
- The supported diagnostic path now distinguishes an empty current model-industry reference from generic missing model
  features.
- Restart recovery reports persisted current-industry coverage before the first successful network refresh.
- Parallel filter/action outcomes are no longer presented as serial filters, and uncomputed downstream stages are no
  longer displayed as business-empty counts.

## Removed

- No data, recommendations, models, snapshots, or history were removed.
- No history downloader, history archive reader/writer, runtime historical-price loader, training sample builder,
  trainer, or model artifact was changed.

## Verification

- Provider, production-model eligibility, input runtime, candidate selection/projection, market feature/reference,
  bootstrap, Web/API/architecture, diagnostic, and JS dashboard regression suites passed. The JS regression first failed
  on the old 24-node rendering and proves that unknown `null` values now render as `—`.
- The real service was normally restarted onto the changed worktree and sampled twice through
  `scripts/diagnose_runtime.py --profile runtime`. The current BaoStock industry refresh succeeded with 5,219 accepted
  rows, 334 invalid rows, no error or timeout, and a 17.27-second supplier latency.
- Against the same 5,277-row live population, Tomorrow advanced from 114 strategy-history rows to 110 model-input rows
  and 67 fully scored rows; D25 advanced through 114, 110, and 71. Today advanced from 91 strategy-history rows to 90
  model-input rows and then truthfully remained at candidate-quote collection after its immutable 11:20 freeze boundary.
  The prior 115-to-zero model-input failure shape did not recur.
- The real 127.0.0.1:5000 service returned the changed `status_view.js`, including the null-safe counter and separated
  main/observation/classification rendering. The isolated Firefox refresh gate passed with 10 DOM samples, a 1.056-second
  maximum interval, 14 ms P95 patch-to-paint, and a successful decision patch.
- Desktop acceptance passed at 1280x720, 1440x900, and 1920x1080 with the complete main line, observation branch, and
  classification totals, no page-level overflow, and no browser errors.
- `make format-check`, `make lint`, `make type-check`, elevated `make package`, the external wheel-install/resource
  check, and `make performance-check` passed. The production performance workload reported zero network calls and no
  budget/equivalence failures.
- `make test` completed the whole suite with no new product-code failure. It remains red only on the same eight
  pre-existing documentation-contract assertions already recorded by the preceding funnel batch: obsolete implementation
  plan/training wording and shared-head statements absent from the pushed baseline. Those unrelated history/training
  contracts were not changed in this batch under the user's explicit exclusion.
- Final staged-diff review and `git diff --cached --check` passed; the user's separate untracked `docs/start.md` remained
  untouched and outside this delivery.

## Residual Risks

- The current-industry supplier is a free external service without an SLA. A cold machine with no persisted reference
  remains fail-closed for industry-aware model inputs until one bounded refresh succeeds; Web and non-model reads remain
  available.
- Independent full-market freshness and runtime liquidity-history coverage can still block an otherwise repaired model
  chain; those earlier funnel stages remain visible and are not relaxed by this batch.
