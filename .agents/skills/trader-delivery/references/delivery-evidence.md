# Delivery evidence

## Evidence depth

For a low-risk single-file, documentation, process, or metadata change with no runtime or public-contract effect, keep the batch lightweight. Record the baseline and preserved user changes, requested outcome, exact scope, direct review/check, ledger update, commit, push, and upstream confirmation. Implement directly; a multi-step plan, architecture comparison, regression test, and live evidence are unnecessary.

For a defect, repeated regression, public-contract change, runtime behavior change, or cross-boundary change, make these facts reviewable:

- review baseline, current branch and upstream relationship, staged/unstaged files, and preserved user changes;
- user-visible symptom or requested outcome;
- a stable `Regression-Key` when the defect family has recurred;
- confirmed evidence and unverified hypotheses;
- first broken boundary, owning component, and downstream consumers selected from the impact matrix;
- target architecture; compare alternatives when ownership, representation, resource orchestration, or timing crosses modules;
- in-scope, permitted collateral, and explicitly excluded behavior;
- applicable contract/regression proof, implementation step, targeted gates, escalation conditions, and live evidence;
- Review baseline, commit scope, push, and confirmation that the local commit reached the upstream branch.

When a plan is warranted, only one plan item may be in progress. A step is complete only when its observable exit condition is satisfied.

## Regression proof

For a defect, preserve evidence at both ends:

1. Reproduce or deterministically model the first broken boundary.
2. Assert the downstream user-visible result, not only an internal counter.
3. Add a negative assertion for the adjacent behavior that must remain unchanged.
4. Re-run the historical failure shape when a `Regression-Key` already exists.

For timing-sensitive current/freeze/Web behavior, cover the applicable freeze scenarios required by `docs/项目重构详细.md`: hot run, cold start, the applicable freeze boundary, 15:00+ recovery, formal-record hit, and permitted close fallback. Record why any scenario is not applicable.

For scoring-chain changes, record the first changed semantic owner and the resulting values at each affected boundary: evidence-quality score, local risk, model diagnostics, execution gate, fused score, action/rank, decision identities, frozen/current/history record, and external projection. Use [the scoring-chain guide](scoring-chain.md); do not collapse these values into a single “score passed” claim.

## Live evidence

Use `scripts/diagnose_runtime.py` according to the runtime guide. Record the profile, bounded configuration, service/release identity, overall status, check statuses, relevant finding codes, and unverified external gate. Do not put prices, tokens, stock-level payloads, personal paths, or full vendor errors in the engineering ledger or handoff.

For a recommendation-funnel incident, also record the six checkpoint results from
[the funnel incident playbook](recommendation-funnel-incidents.md): host-network reachability, the first failed refresh
stage, timezone ownership, per-stage funnel counts and primary blocker, the applicable freeze/recovery cell, and proof
that the restarted real service imported the changed release. Preserve both the pre-fix failure shape and the post-fix
stage counts; do not summarize every zero as “no data”.

## Review and handoff

- Compare the complete diff with the previously pushed baseline and with the original file allowlist.
- Inspect new files, removed paths, duplicate owners, hidden fallbacks, TODOs, generated output, and source-file size.
- Run `git diff --check`; confirm the staged set contains only this batch.
- Record actual changes, verification, and unfinished work only in `docs/03_工程实施.md`. For a repeated defect, connect its stable `Regression-Key`, confirmed cause or `pending verification`, behavior change, verification, and residual risks.
- Do not mark the batch complete before its single commit is pushed and confirmed present on the upstream branch.
