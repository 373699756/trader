# Delivery evidence

## Mode boundary

Apply this reference within the current task mode. Planning produces read-only
findings and a plan; read-only review/diagnosis produces findings and evidence
gaps. Neither starts an implementation stage or performs edits, commits or
pushes. The implementation and delivery requirements below apply only to an
authorized implementation task. A failed or unavailable required gate remains
explicit; do not report completion because the skill was read or a command was
attempted.

## Evidence depth

For a low-risk single-file, documentation, process, or metadata change with no runtime or public-contract effect, keep the batch lightweight. Record the baseline and preserved user changes, requested outcome, exact scope, direct review/check, commit, push, and upstream confirmation. Update the engineering ledger only when `AGENTS.md` requires a stage or an existing task already owns one. Implement directly; a multi-step plan, architecture comparison, regression test, and live evidence are unnecessary.

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

When a defect changes key behavior or carries material regression risk, preserve evidence at both ends. Keep the original failure evidence, complete the implementation slice, then add the minimum necessary regression coverage; ordinary documentation and low-risk cleanup do not require new tests.

1. Reproduce or deterministically model the first broken boundary.
2. Assert the downstream user-visible result, not only an internal counter.
3. Add a negative assertion for the adjacent behavior that must remain unchanged.
4. Re-run the historical failure shape when a `Regression-Key` already exists.

For timing-sensitive current/freeze/Web behavior, cover the applicable freeze scenarios required by `docs/2_工程设计.md`: hot run, cold start, the applicable freeze boundary, 15:00+ recovery, formal-record hit, and permitted close fallback. Record why any scenario is not applicable.

For scoring-chain changes, record the first changed semantic owner and the resulting values at each affected boundary: evidence-quality score, local risk, model diagnostics, execution gate, fused score, action/rank, decision identities, frozen/current/history record, and external projection. Use [the scoring-chain guide](scoring-chain.md); do not collapse these values into a single “score passed” claim.

## Live evidence

Use `scripts/diagnose_runtime.py` according to the runtime guide. Record the profile, bounded configuration, service/release identity, overall status, check statuses, relevant finding codes, and unverified external gate. Do not put prices, tokens, stock-level payloads, personal paths, or full vendor errors in the engineering ledger or handoff.

For a recommendation-funnel incident, also record the six checkpoint results from
[the funnel incident playbook](recommendation-funnel-incidents.md): host-network reachability, the first failed refresh
stage, timezone ownership, per-stage funnel counts and primary blocker, the applicable freeze/recovery cell, and proof
that the restarted real service imported the changed release. Preserve both the pre-fix failure shape and the post-fix
stage counts; do not summarize every zero as “no data”.

## Review and handoff

Check the evidence at the operation it describes; do not replace it with a
checklist of unsupported "done" statements:

| Claim | Evidence to inspect |
| --- | --- |
| Consumers switched | Definition and consumer searches before/after, imports, exports, entry strings and patch targets; classify any remaining old-name matches |
| Ownership migrated | New owner and all consumers in the diff, old active paths actually deleted; no alias, forwarding or parallel state |
| Gate passed | This batch's exact command, exit result and relevant output; distinguish failures, unrun checks and baseline failures |
| Live behavior verified | Changed release/process identity and applicable real probe; fixture, HTTP 200 and source inspection cannot establish this |
| Review complete | Full diff including added/deleted/untracked files against the pushed baseline, no unresolved in-scope findings |
| Implementation delivered | Exact staged scope; actual commit message checked against AGENTS.md; one commit, successful push and local/upstream commit equality |

- Compare the complete diff with the previously pushed baseline and with the original file allowlist.
- Inspect new files, removed paths, duplicate owners, hidden fallbacks, TODOs, generated output, and source-file size.
- For Python changes, review naming and migration against [the Python engineering skill's single naming contract](../../python-engineering-guide/references/coding-standard.md#3-命名格式与可读性).
- In a shared worktree, capture the initial staged set separately from unstaged files. Before commit, re-read `HEAD`, its upstream and `git diff --cached --name-only`; if another batch advanced the branch or touched a shared file, rebase the review baseline and stage only this batch's exact hunks.
- Run `git diff --check` and `git diff --cached --check`; confirm every staged hunk belongs to the original file allowlist.
- When a stage is required, record actual changes, verification, and unfinished work only in its existing `docs/3_工程实施.md` entry. Lightweight batches follow `AGENTS.md` without creating a stage for formality. For a repeated defect, connect its stable `Regression-Key`, confirmed cause or `pending verification`, behavior change, verification, and residual risks.
- After implementation, Review and required checks finish, mark the existing stage `completed` before committing as AGENTS.md requires. Report delivery complete only after the single commit is pushed and confirmed present on the upstream branch; a failed push leaves delivery pending.
