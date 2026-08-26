# Delivery evidence

## Plan fields

Every change plan must make these facts reviewable:

- baseline commit, upstream commit, staged/unstaged files, and preserved user changes;
- user-visible symptom or requested outcome;
- recurrence search terms and a stable `Regression-Key` for a repeated defect family;
- confirmed evidence and unverified hypotheses;
- first broken boundary, owning component, and downstream consumers selected from the impact matrix;
- target architecture, rejected alternative, and why the selected design is better;
- in-scope, permitted collateral, and explicitly excluded behavior;
- failing contract/regression test, implementation step, targeted gates, escalation conditions, and live evidence;
- Review baseline, commit scope, push, and `HEAD == @{upstream}` confirmation.

Only one plan item may be in progress. A step is complete only when its observable exit condition is satisfied.

## Regression proof

For a defect, preserve evidence at both ends:

1. Reproduce or deterministically model the first broken boundary.
2. Assert the downstream user-visible result, not only an internal counter.
3. Add a negative assertion for the adjacent behavior that must remain unchanged.
4. Re-run the historical failure shape when a `Regression-Key` already exists.

For timing-sensitive current/freeze/Web behavior, cover the applicable matrix from `docs/software-business-design.md` section 13.1: morning hot run, midday cold start, 11:20 and 14:50 boundaries, 15:00+ hot run/cold recovery, formal-record hit, and permitted close fallback. Record why any cell is not applicable.

## Live evidence

Use `scripts/diagnose_runtime.py` according to the runtime guide. Record the profile, bounded configuration, service/release identity, overall status, check statuses, relevant finding codes, and unverified external gate. Do not paste prices, tokens, stock-level payloads, personal paths, or full vendor errors into Changelog.

## Review and handoff

- Compare the complete diff with the previously pushed baseline and with the original file allowlist.
- Inspect new files, removed paths, duplicate owners, hidden fallbacks, TODOs, generated output, and source-file size.
- Run `git diff --check`; confirm the staged set contains only this batch.
- In `CHANGELOG.md`, connect the symptom, `Regression-Key`, confirmed cause or `pending verification`, behavior change, verification, and residual risks.
- Do not mark the batch complete before its single commit is pushed and local/upstream hashes match.
