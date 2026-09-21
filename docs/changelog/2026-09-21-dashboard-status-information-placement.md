# Reposition dashboard market and score status

## User request

Move the current strategy, quote source, freshness, quote time and score time to the top header before the
runtime, stream and observation controls. Remove the score range, highest-scoring stocks, score-scale summary
and final funnel counts from the observation drawer, and show them in the main data-status card instead.

Regression-Key: `dashboard-status-information-placement-v1`

## Cause and current-state judgment

The page bound market freshness to the data-status card while the score summary lived in the observation
drawer. This split the most frequently scanned status across two locations, and the quote timestamp was
updated by JavaScript but visually hidden. The state projection and score calculation were already correct;
the defect was limited to Web ownership and layout.

## Changed

- The existing market-status DOM nodes now live in the header before the runtime, stream and observation
  controls. Quote time is visible alongside source age, and narrow screens give this status its own first row.
- The score range, Top 3 list, score-scale summary and final funnel counts now live in the main data-status
  card. The observation drawer retains the 14-stage diagnostics and global runtime issues without duplicating
  the score summary.
- The redundant compact Top-score renderer and DOM node were removed; the existing canonical Top-score
  renderer remains the only owner.
- Desktop acceptance now asserts the new DOM ownership and visible quote time at 1280x720, 1440x900 and
  1920x1080. Its obsolete error-drawer selectors, removed runtime-message expectation and initialization race
  were aligned with the current observation drawer.

## Verification

- `node tests/js/test_dashboard_state.js` passed.
- `.venv/bin/python3 -m pytest tests/contract/test_app_factory.py tests/contract/test_web_contract.py -q`
  passed with 11 tests.
- Ruff for the affected Python contract and browser acceptance files, `compileall` for `src/trader` and
  `tests`, and `git diff --check` passed.
- The isolated headless Firefox desktop acceptance passed at 1280x720, 1440x900 and 1920x1080 with no page
  overflow or browser errors. It confirmed the header ordering, visible quote time, score-summary ownership in
  the data-status card, absence from the observation drawer, and rendered Top-score identities. The fixture
  made no external network calls.

## Residual risks

- The browser run used the repository's isolated typed fixture, not the active local runtime or a live market
  supplier. No API schema, scoring rule, runtime state, supplier integration or activity data was changed.
