# Delivery Record: Invalid Quote Readiness and Publication

## User request

Continue the unfinished TODO requirements: missing or invalid market inputs must
remain readiness gaps, with truthful stage counts and safe decision publication.

## Cause

Confirmed by the existing failing projection regression: a missing candidate
price returned publishable `business_empty`. The filter registry changed the
same invalid price/amount/OHLC/percentage input from `DEFERRED` to `REQUIRED`
after directed refresh. Quality assessment then excluded business-rejected
evaluations from transient-gap detection. The scoring document retained the
same inconsistent finalized-input rule. Mixed business rejection and missing
inputs could also double-count one stock as rejected and pending.

Regression-Key: `market-epochs-complete-pipeline-snapshots-v1`.

## Behavior change

- Remove the finalized-input severity switch and all callers. Invalid quote
  fields consistently defer in population, directed-candidate and dynamic filters.
- Preserve confirmed business rejection priority; otherwise stale/future inputs
  take refresh-pending priority over data-pending. Counts describe exclusive
  terminal states while field audits retain all observed reasons.
- An empty result with unresolved invalid inputs is not publishable. Production
  raises `DecisionUnavailableError`, retains prior valid projections and only
  keeps an ephemeral diagnostic draft. Confirmed business-empty results remain
  publishable, and an eligible stock is not blocked by another stock's bad input.
- Synchronize the scoring authority and current engineering description. No
  formula, numeric threshold, model, DeepSeek, freeze algorithm or schema changes.

## Verification

The historical missing-price regression failed before implementation and passes
afterwards for Tomorrow and D25. Expanded filtering, selection/fusion, projection,
input-runtime, stage, current/stream, static/market and document/Web contracts:
273 passed, 2 failed. Two additional partial-input production cases and 19
point-in-time dataset/recall/ablation cases passed: 294 distinct passing cases.

Both failures were independently reproduced against pushed baseline `19b9dd37`
in a repository-external isolated copy: the draft-freeze test still asserts the
retired 14:50 boundary, and the scoring-document test still caps length at 900
lines (baseline already has 1030). Neither is fixed or reported as passing.

Regression proof covers the filter boundary, missing/non-finite/structurally
invalid fields, candidate capacity, mixed rejection/gaps, exclusive pending
states, 14-stage status and the final production refusal. Production refusal and
prior-projection retention were exercised at 09:25, 14:40, 14:59:59, 15:00 and
15:05 for both scored strategies. These are controlled input/publication tests,
not a formal freeze/restart acceptance. Native input fingerprint validation is
unchanged; non-finite filter fixtures do not relax the JSON identity boundary.

Affected eight Python files pass Ruff/format-check; four sources pass mypy using
`.venv/bin/python3 -m mypy` (the installed console script has a stale interpreter
path). Complete diff review and `git diff --check` passed.

## Live evidence and residual risks

Bounded read-only `runtime` diagnostics used two samples, 0.1-second interval,
2-second HTTP timeout and 20-second command timeout. Sandbox and host-network
contexts both returned `connection_failed`, zero successful samples and no
release identity. No service was restarted and no supplier quota was spent.

The six incident checkpoints are bounded as follows: reachability unavailable;
the first semantic failure confirmed at filter classification; timezone handling
unchanged and Shanghai fixture ownership verified; pre/post missing-price counts
change from one candidate rejection to zero rejections and one pending input;
freeze-window input tests pass but formal hit/fallback and hot/cold recovery
remain unverified; changed-release live-process evidence unavailable.

Independent complete static population, real stage 5–9 orchestration and removal
of synthetic latency/health, all-attempted-source health, long-term history
coverage, supplier/browser/freeze/undo live gates and return research remain open.
The two baseline test failures require separate contract-calibration work.
The user's candidate-ratio TODO remains outside this commit.
