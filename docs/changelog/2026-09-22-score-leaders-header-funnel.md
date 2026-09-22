# Separate score leaders and restore the header funnel summary

## User request

Keep the four dashboard summary cards at the original compact height, give the score range and Top 3 stocks
their own wider, visually emphasized card, and restore the complete-score/action-qualified/final-pool/formal/
observation counts after the header market status.

Regression-Key: `score-leader-header-funnel-layout-v1`

## Cause and current-state judgment

The data-status card owned input readiness, score range, Top 3 and funnel counts at the same time. This made the
highest-value information compete for one compact area. The funnel count element was also only assigned after a
fully progressed pipeline, so pending states could leave it empty or stale. Score and API data were already
correct; the issue was Web information ownership and incomplete state projection.

## Changed

- Split the desktop summary into four equal-height cards: data status, score leaders, model budget, and publication status.
- Gave the score-leader card more horizontal space and a restrained blue accent while keeping the existing 78px card height.
- Kept the final-score range and up to three ranked stocks together, with one stock per line and no single-line ellipsis.
- Moved the five-stage funnel aggregate next to the strategy/source/freshness/score-time header status, preserving explicit zeroes and rendering unavailable counts as `—`.

- No score, filter, TopK, observation, freeze, publication, API, SSE, or persistence contract changed.
- Existing DOM identifiers remain the state-projection boundary; only their visible ownership and layout changed.

## Verification

- JavaScript syntax and dashboard-state contracts passed.
- Web app-factory, recommendation-section and HTTP/Web contracts passed with 13 tests; affected Ruff checks passed.
- Isolated headless Firefox acceptance passed at 1280x720, 1440x900 and 1920x1080 with four equal-height cards,
  no page overflow, no browser errors, no external network calls and correct DOM ownership.
- `git diff --check` passed.

## Residual risks

- Browser evidence uses the repository fixture rather than the active runtime or live supplier data. Very narrow
  screens wrap the header funnel onto its own line; the supported desktop viewports keep it on the status line.
