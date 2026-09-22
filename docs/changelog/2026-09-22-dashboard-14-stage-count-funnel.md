# Show the 14-stage funnel counts on the dashboard

## User request

Replace the compact five-count header summary with the decreasing counts from the existing 14-stage recommendation
snapshot, and remove the blue vertical line from the score-leader card.

## Changed

- The header now reads the existing Web `stage_snapshots` projection and renders all 14 output counts as an arrow
  chain, followed by formal and observation counts.
- Missing stage snapshots remain visibly unknown as `—`; real zero counts remain `0`.
- Removed the score-leader card's blue inset border.
- No backend, scoring, API/SSE, persistence, or runtime behavior changed.

## Verification

- Dashboard JavaScript state contract passed.
- Web contract and desktop fixture tests passed.
- Ruff, Python compile, JavaScript syntax, and `git diff --check` passed.
