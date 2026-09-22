# Bound Routine History Maintenance to Changed Data

## User request

Keep an existing 2000-session archive usable for training without repeatedly scanning the full 13 GiB archive or
redownloading a complete per-security window when routine maintenance only needs roughly one or two weeks of data.

## Evidence

- Routine synchronization verified every active monthly partition before incremental planning, verified the recent
  month again before reading qfq identities, and verified every unchanged month again during publication.
- Each partition verification reads the complete file for SHA-256, runs SQLite `quick_check`, and counts all rows.
- A changed qfq value in the five-session reread window caused a second supplier request for that security's complete
  2000-session window.
- Raw and qfq are not duplicate representations: qfq supplies training and return features, while raw supplies
  tradability, suspension, price-limit, and outcome facts. BaoStock exposes them through separate per-security calls.

## Behavior change

- Routine synchronization trusts immutable partition references already owned by the active snapshot. It no longer
  performs a complete preflight verification of every active month.
- Snapshot publication reuses unchanged monthly references without revalidating their files. New or changed monthly
  databases still perform WAL checkpoint, SQLite integrity checks, row-count verification, SHA-256 calculation,
  rollback-sidecar replacement, and atomic active-snapshot publication through the partition sealing owner.
- Recent qfq, industry, or supplier-contract changes are written only as revisions in the bounded reread window and
  no longer trigger an automatic complete-window request for existing securities. Newly listed securities still fetch
  their available complete window once.
- Training, repack, explicit diagnostics, recovery of interrupted replacements, raw/qfq semantics, supplier request
  spacing, and the five-session reread contract remain unchanged.

## Verification

- Targeted synchronization regression coverage proves an already-current run performs no partition verification,
  a changed qfq value stays within the recent request window, and an incremental publication does not verify an
  untouched middle month.
- Existing synchronization coverage continues to exercise initial sealing, checkpoint resume, supplier failure,
  cancellation, publication rollback, interrupted replacement recovery, and immutable-month reuse.

## Residual risks

- BaoStock still requires separate raw and qfq requests per security and the established two-second safe interval, so
  a full-market daily update remains supplier-bound. Removing either side would violate the scoring and outcome
  contracts; a faster source requires separate capability evidence.
- Routine synchronization now detects corruption in an unchanged partition when that partition is actually read, or
  when training, repack, recovery, or explicit diagnostics verifies the snapshot, rather than scanning every file on
  every maintenance start.

Regression-Key: `history-incremental-maintenance-bounded-io-v1`
