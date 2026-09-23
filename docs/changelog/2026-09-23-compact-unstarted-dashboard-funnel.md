# Delivery Record: Compact the Unstarted Dashboard Funnel

## User request

The dashboard rendered an unstarted 14-stage recommendation chain as fourteen
dash placeholders joined by arrows, followed by zero formal recommendations.
The empty state was visually noisy and hard to scan.

## Cause

The Web projection correctly recognized that scoring had not started, but its
header summary still formatted every unknown `output_count` independently.
The resulting placeholder chain communicated no additional state.

## Behavior change

- When all 14 stage output counts are unknown and scoring has not started, the
  header displays `14 层链路待启动` followed by the existing pool counts.
- If scoring has progress but canonical stage counts are not yet projected, the
  compact label is `14 层链路计数待就绪` instead of falsely claiming no start.
- When any stage has a real count, the full canonical 14-stage chain remains
  visible, including real zeroes and per-stage unknown markers.
- No backend data, scoring state, API schema, or stage ownership changed.

## Verification

- Dashboard JavaScript state contract covers both compact empty and populated
  14-stage summaries.
- Targeted Web contract verification and diff checks passed.
- The Firefox desktop gate passed at 1280x720, 1440x900, and 1920x1080 with no
  browser errors, page overflow, or summary-card overlap.

## Residual risks

- The browser gate used deterministic local fixtures. Live supplier and scoring
  readiness remain outside this presentation-only batch.

`Regression-Key: empty-funnel-summary-compact-v1`.
