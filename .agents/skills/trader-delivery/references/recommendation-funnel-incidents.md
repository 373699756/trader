# Recommendation-funnel incident playbook

Use this playbook for empty Web data, a stuck collecting state, unexpected recommendation-funnel counts, or a mismatch
between scheduler status and the page. It captures the failure family behind
`refresh-completed-at-shanghai-v1`; it does not assert that every future incident has the same cause.

Complete the checkpoints in order. Record each result as confirmed, ruled out, or unavailable. Do not stop after the
first plausible explanation.

## 1. Checkpoint: host-network-reachability

Run the unified `runtime` profile first. A sandbox or container may have a different loopback/network namespace from the
desktop service, so `connection_failed` proves only that the probe could not reach the configured URL from that execution
context. It does not prove that no service is running.

- Compare the expected service process/listener and the diagnostic execution context without reading secrets.
- If a real service exists outside the sandbox, rerun the same `scripts/diagnose_runtime.py` command in the service's host
  network context with the required approval. Do not replace it with a new wrapper or one-off probe.
- Record the runtime/release identity returned by the reachable service. If it cannot be read, keep live verification
  explicitly unavailable.
- Never turn a sandbox `connection_failed` into a product root cause or a claim that port 5000 is unused.

## 2. Checkpoint: stage-by-stage-refresh

Trace the first broken owner instead of treating the final Web symptom or a generic category as the cause. Inspect these
stages independently:

1. supplier request and response classification;
2. normalization and feature construction;
3. immutable market/feature publication;
4. typed `V2RefreshOutcome` construction;
5. scheduler task settlement and downstream scoring submission;
6. decision publication, status projection, API/SSE, and Web rendering.

An earlier stage can commit successfully and a later stage can still fail. In the recorded incident, market features were
already published, but typed outcome construction raised `refresh:value_error`; the `close_quotes` task therefore retried
and never submitted Tomorrow/D25 close recovery. Do not infer “supplier failed” from the task's final failure category, and
do not infer “refresh succeeded end to end” from populated market features alone.

Generic codes such as `refresh:value_error` are localization clues, not sufficient root-cause evidence. Reproduce the
owning typed boundary with the same task shape and sanitized typed inputs. Extend the existing diagnostic owner when the
probe is reusable; do not persist raw vendor payloads or create a competing top-level script.

## 3. Checkpoint: timezone-normalization

All business/application completion times are owned in `Asia/Shanghai`, while supplier or infrastructure timestamps may
arrive in UTC. Python can compare aware datetimes with different offsets, but `max(...)` returns the original winning
object and therefore preserves its original timezone.

- Reject naive datetimes at the input boundary.
- Compare instants only after confirming awareness, then explicitly project the selected completion instant to
  `Asia/Shanghai` before constructing application state such as `V2RefreshOutcome`.
- Do not weaken the Shanghai-time typed value-object contract or add dual UTC/Shanghai representations.
- Regression tests must make a later UTC `received_time` win over a Shanghai request/feature time and assert both the
  absolute instant and the final timezone.
- Review every dynamic `max/min/sort` selection of mixed-source timestamps for the same return-object trap.

## 4. Checkpoint: funnel-semantic-classification

Read one strategy and one trade date from the typed scheduler input-quality projection. Classify every stage separately:

```text
requested_candidates
  -> candidate_features
  -> security_master
  -> history
  -> full_scored
  -> selected_executable / selected_observe
```

- `candidate_quotes_pending` means candidate feature collection has not completed; render it as collecting, not as a
  confirmed numeric zero.
- `security_master_coverage_incomplete` means candidate features may be complete while the reference-data quality gate
  legitimately prevents formal publication.
- A nonzero `full_scored` with zero selected is not automatically data loss: the strategy may legally return 0 after
  filters, score/action thresholds, risk, TopK, board, or industry constraints.
- Historical readiness is per stock and per strategy/profile. A coverage ratio is a health metric, not permission to
  discard scores that already exist. If `full_scored > 0` while a legacy `history_coverage_incomplete` batch blocker
  prevents publication, classify `global_history_gate_blocked_eligible_candidates`; do not tune the percentage.
- Check the declared lookback against the active score implementation. Tomorrow V1/V2 use 20/40/60-session skip-5
  features and therefore require 61 valid qfq sessions per scored stock; a generic 20-session batch flag neither proves
  model eligibility nor justifies blocking other eligible stocks.
- A zero formal TopK must not imply zero research evidence. Profit-oriented V1/V2 comparison needs same-day, same-stock,
  same-input paired predictions for every mutually eligible candidate, followed by outcome labels without production
  authority; selected-only outcomes are selection-biased.
- Compare `primary_blocker`, filter counts, highest score, formal/observe counts, and the same-strategy/same-trade-date
  current projection before calling a zero abnormal.
- Never fabricate a downstream count, derive one by subtracting non-exclusive reason counts, or change the Web to hide a
  real upstream gate.

The incident's post-fix Tomorrow `360/360/120/78/56/0` and D25 `360/360/120/79/58/0` funnels proved that refresh
settlement recovered while `security_master_coverage_incomplete` remained a separate, genuine data-quality blocker.

## 5. Checkpoint: freeze-window-control

Classify the observation using the authoritative hot/cold five-period matrix before labeling `not_ready` as a regression.

- Today cannot be backfilled after 11:20. A cold start after the freeze remains `not_ready`; only quote overlay may change
  an existing formal record.
- Tomorrow/D25 freeze at 14:50. When the same-day formal record is missing, 15:00+ recovery may freeze the current V2 run
  or create the permitted local `close_fallback`; it must not call DeepSeek or overwrite an existing formal record.
- Inspect the `close_quotes` refresh state separately from Today. A failed close refresh can block permitted Tomorrow/D25
  recovery while Today's non-backfill remains correct.
- Record which time-window cell was exercised and which cells are not applicable; one timestamp fixture is insufficient
  for a behavior change.

## 6. Checkpoint: current-release-restart

For a repair, final evidence must come from the real service after a normal restart onto the changed release. A diagnosis-
only task must instead mark this checkpoint unavailable and must not claim the incident is fixed.

- Confirm the process imported the expected release/runtime identity, not only that a source file changed on disk.
- Run multiple bounded `runtime` samples and require the affected refresh process to settle without the historical
  failure shape. Capture relevant stage counts and the remaining primary blocker.
- Use `full` when browser delivery or performance is affected, but remember that its isolated browser fixture does not
  prove the already-running desktop service loaded new code.
- When Web behavior/layout changes, run the repository desktop gate at 1280x720, 1440x900, and 1920x1080.
- Keep the service's controlled supplier degradation distinct from a failed product gate.

## Closure evidence

A funnel incident repair is closed only when the handoff states all of the following:

- which execution context could or could not reach the service;
- the first broken stage and why later symptoms followed;
- whether mixed-source timestamps crossed a typed timezone boundary;
- the pre-fix and post-fix stage counts for the same strategy/date;
- the real remaining blocker, including why a final zero is valid or still defective;
- the applicable freeze/recovery behavior;
- the restarted service/release identity and bounded runtime result;
- the failing regression before implementation and passing regression after it.

Do not close from source inspection, HTTP 200 alone, mocks, a generic exception label, a browser fixture, or a single
aggregate `0`.
