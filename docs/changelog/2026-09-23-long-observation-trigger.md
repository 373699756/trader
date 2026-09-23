# Delivery Record: Disable the Scoring Observation Trigger for Long

## User request

The Long page exposed the Header observation button and opened the short-horizon
scoring monitor even though Long is an unscored fixed research watchlist. The
user also asked whether the unstarted recommendation chain was caused by qfq
history gaps and why runtime recovery had not populated it.

## Cause

The observation trigger was always visible and its drawer controller did not
check the selected strategy. Runtime diagnosis found a separate live incident:
Tomorrow and D25 had no pipeline or candidate population, while market refresh
reported unavailable candidate/intraday universes and a data deadline. The
history archive was also unavailable because an active `download` process still
owned the maintenance lock and had not atomically published a snapshot.

History recovery was present but was not the first broken boundary. It only
receives missing codes after a candidate population exists, fetches at most 120
codes per bounded batch, keeps results in memory, and never publishes the
offline archive. With zero candidate codes, there was nothing to recover.

## Behavior change

- Selecting Long now disables the observation trigger using native button
  semantics, labels it as not applicable, and closes any open scoring monitor.
- The drawer controller rejects activation while disabled; switching back to
  Tomorrow or D25 restores the trigger and its existing diagnostics.
- No supplier, candidate, scoring, qfq, archive, or recovery behavior changed.

## Verification

- Dashboard JavaScript state and targeted Web contracts passed.
- The deterministic Firefox desktop gate verifies that Long cannot open the
  monitor, Tomorrow can still open it, and all three supported viewports remain
  free of browser errors, overflow, and overlap.
- The host-network runtime diagnostic reached the live service for three
  samples. Source probes separately confirmed the security master, Tencent
  quotes, and configured Tushare raw daily access were reachable.

## Residual risks

- The running service was started before this Web commit and must be normally
  restarted to load the changed assets.
- The live `download` remained active during diagnosis, so snapshot publication
  and the later candidate/scoring recovery were not observed to completion.
- Tushare lacks qfq adjustment-factor access, and no validated full-market daily
  raw/qfq batch source is configured; BaoStock maintenance remains per security.

`Regression-Key: long-observation-trigger-disabled-v1`.
