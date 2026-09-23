# Delivery Record: Early-Listing BaoStock QFQ Reconstruction

## User Request

`./run.sh download` failed on `sz.001239` with `supplier_data_incomplete` after BaoStock returned an unadjusted row for the stock's first qfq session.

## Cause

The BaoStock row gateway treated a qfq response row with `adjustflag=3` as an invalid row and dropped it. The synchronizer then required every expected session to contain a qfq side, so one explicit supplier adjustment gap aborted the whole synchronization.

## Changed

- Qfq rows explicitly returned with `adjustflag=3` are recorded as supplier adjustment gaps rather than generic malformed rows.
- A gap is reconstructed only when the same stock has raw rows with positive `preclose` and `close` values and a later valid qfq/raw anchor. The factor is propagated backward with BaoStock's six-decimal `ROUND_HALF_UP` rule.
- Reconstructed rows scale only same-source raw OHLC; raw volume, amount, and trading status remain unchanged. Other malformed rows and unclosed reconstruction chains still fail closed.

## Verification

- BaoStock gateway, history synchronization, and CLI tests: `34 passed`.
- Ruff, format check, mypy, compileall, and `git diff --check` passed for the affected files.

## Residual Risks

- No live full-market download was run. The real `sz.001239` response, end-to-end duration, and active snapshot publication remain unverified in this batch.
- This does not authorize arbitrary raw-to-qfq derivation or cross-source blending.

`Regression-Key: baostock-early-listing-qfq-reconstruction-v1`.
