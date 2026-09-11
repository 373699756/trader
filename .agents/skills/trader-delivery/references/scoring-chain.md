# Scoring-chain delivery guide

Read this guide whenever a task changes candidate eligibility, evidence-quality scoring, a scoring profile, model
diagnostics, execution cost, risk, DeepSeek fusion, action thresholds, ranking, score identity, or offline score evidence.
The authoritative rules remain `docs/01_评分逻辑.md`; architecture, freeze, API/SSE, Web, and acceptance remain in
`docs/02_工程设计.md`.

## Trace semantic owners before editing

Identify the first changed owner and trace only the affected paths through this sequence:

```text
point-in-time population and candidate eligibility
  -> weighted evidence-quality base_score
  -> local_score after local_risk_penalty exactly once
  -> model prediction and model_prediction_rank diagnostics
  -> model_net_utility_non_positive execution gate
  -> optional structured DeepSeek review and fixed fusion
  -> action pools, stable ranking, TopK and concentration
  -> decision input identities
  -> freeze/persistence/current/history
  -> GET/SSE/Web and offline outcome interpretation
```

Do not merge owners to simplify the trace. Tomorrow model prediction and relative rank are diagnostics and cost-gate
inputs; they do not replace the three short-horizon strategies' evidence-quality `base_score`. Execution cost can block
an action but cannot rewrite a score. DeepSeek free text cannot directly change a penalty.

## Preserve production invariants

- New Today, Tomorrow, and D25 decisions identify the shared 0–100 evidence-quality scale with
  `score_scale=weighted_evidence_quality_0_100`. The 0/50/100 anchors keep the same adverse/neutral/favorable meaning even
  though each holding period owns different factors and weights.
- Keep `model_prediction_rank` separate from `base_score`, `local_score`, and `final_score`. Changing the batch population
  may change the relative model diagnostic but must not silently redefine the production score.
- Apply `local_risk_penalty` once. Preserve
  `clamp(local_score * 0.68 + deepseek_score * 0.32 - deepseek_risk_penalty, 0, 100)`, use `ROUND_HALF_UP` to two
  decimals, and keep the golden `83.40` vector. Model cost, local risk, and DeepSeek risk each retain their own owner and
  must not be double-counted.
- `model_net_utility_non_positive` prevents DeepSeek review, observation, and formal selection while preserving the
  evidence-quality score and model diagnostics for explanation. Missing required model input or a mismatched prediction
  batch fails closed; it does not fall back to a fabricated neutral prediction.
- Trace `score_scale`, score-model identity, and input hashes through local/hybrid decisions, first-wins freezing,
  persistence codecs, current/history queries, GET, SSE replacement/resync, and Web rendering. Legacy Tomorrow frozen
  records without `score_scale` keep their original relative-score meaning and are never relabeled, backfilled, or
  migrated in place.
- Long remains outside the score, DeepSeek, TopK, and freeze chain.
- Only V1/V2/V3 may name user-selected scoring profiles. Do not create another project version identity for a score-scale,
  API, event, report, cache, dataset, or runtime change.

## Choose proof by the changed meaning

Use contracts and failing tests before implementation. Cover the first semantic owner and the final affected consumer;
add the adjacent negative assertion that proves a neighboring owner did not change.

- Evidence-score formula, factor, or weight: test per-strategy component ownership, 0/50/100 anchors, stable ordering,
  configuration as the single numeric source, risk once, and `83.40` when fusion is reachable.
- Model/profile or cost gate: test feature eligibility, prediction batch identity, relative diagnostic isolation, positive
  and non-positive net utility, cache/deadline behavior, and the absence of a score override or silent fallback.
- Risk, fusion, action, or ranking: test structured evidence, veto, budget/degradation behavior, both action pools,
  deterministic ties, TopK, board/industry concentration, and unchanged score values where only eligibility changes.
- Identity, freeze, or persistence: test new-record identities, local-to-hybrid parentage, first-wins behavior, codec/hash
  round trips, current and historical reads, plus explicit legacy-record semantics. Exercise the applicable 11:20 and
  14:50 boundaries, late results, hot/cold starts, and permitted close fallback from the five-period matrix.
- API/SSE/Web: test serializer whitelists, GET and full SSE replacement parity, reconnect/resync, new and legacy score
  labels, cost-gated empty results, and desktop browser behavior when visible output changes.
- Hot-path changes: run the fixed production performance workload and prove `network_calls=0`; do not infer production
  latency from a small unit fixture.

Any production score formula, factor, weight, threshold, model, capacity, or ranking change that claims better investment
results must separately satisfy the point-in-time, cost-aware research gates in `docs/01_评分逻辑.md` section 12. Record
confirmation, terminal holdout, Shadow, and explicit human authorization separately. Unit tests, a prettier distribution,
more recommendations, or a higher average score do not prove improved return and cannot grant production authority.
