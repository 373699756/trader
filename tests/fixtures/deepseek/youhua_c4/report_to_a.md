# Youhua C4 Report to A

```text
codex_and_phase: Codex C / C4.1-C4.4
base_commit: 7a8a0282d025cbc23fffff5736e94c2d1bf883e0
head_commit_or_patch: patch only; no C-owned commit or push in shared worktree
owned_paths_changed:
  - src/trader/infra/deepseek/budget.py
  - src/trader/infra/deepseek/reviewer_support.py
  - tests/component/test_youhua_deepseek_c4.py
  - tests/fixtures/deepseek/youhua_c4/report_to_a.md
contract_assumptions:
  - docs/reports/youhua-g3-gate-review.md publishes G3 and leaves stage 4 for the next user instruction.
  - C4 verifies and repairs only DeepSeek cost, atomic counting, conservative facts/challenger behavior and result-republication performance.
  - Public ports, Polars, P6/Web production code, shared documents, CHANGELOG and final G4 publication remain A-owned.
schema_or_migration_changes:
  - none; DeepSeek facts schema remains deepseek_v4_review_facts_v1 and SQLite schema remains version 3.
tests_run_and_results:
  - .venv/bin/python -m pytest tests/component/test_youhua_deepseek_c4.py -q: 7 passed.
  - .venv/bin/python -m pytest tests/unit/test_v2_deepseek_base.py tests/component/test_v2_deepseek.py tests/component/test_v2_deepseek_v4.py tests/component/test_youhua_deepseek_c2.py tests/component/test_youhua_deepseek_c3.py tests/component/test_youhua_deepseek_c4.py -q: 98 passed.
  - .venv/bin/python -m pytest tests/unit/application/test_recommendations.py tests/unit/domain/test_fusion.py tests/unit/domain/test_downside.py tests/unit/domain/test_risk.py tests/unit/domain/test_risks.py -q: 40 passed.
  - .venv/bin/python -m ruff check src/trader/infra/deepseek tests/component/test_youhua_deepseek_c4.py: passed.
  - .venv/bin/python -m mypy src/trader/infra/deepseek src/trader/domain/review tests/component/test_youhua_deepseek_c4.py: passed (23 source files).
performance_before_after:
  - C4 fixed fixture publishes 30 consecutive schema-v2 recommendation patches carrying 8 reviewed stocks and asserts nearest-rank P95 <= 1.0 second.
  - No production network latency is claimed; the C4 timing isolates validated DeepSeek result to in-process recommendation patch construction/SSE enqueue work with tracemalloc disabled.
known_failures_and_risks:
  - Existing tests using fixture model name "model" still emit the known non-failing model-catalog warning.
  - Real DeepSeek service latency and the full A4/G4 cross-domain performance scenario remain A-owned external/full-gate evidence.
  - The shared worktree contains concurrent non-C changes in application/Web/market-data paths; C did not edit or include them in this handoff.
requested_interface_changes:
  - none
ready_for_gate: yes
```

## C4.1 Cost Envelopes and Global Hard Limit

- The production budget vector admits exactly 58 normal attempts: shared preheat 10, today 22, tomorrow 14 and d25 12.
- Eight challenger attempts remain separate from the normal soft buckets, producing the planned maximum of 66 attempts before emergency.
- Five emergency attempts become available only after the owning strategy's normal primary soft bucket is exhausted, producing the planned maximum of 71 attempts.
- A 224-request concurrent probe admitted exactly 188 atomic reservations and rejected the remaining 36 with `daily_hard_limit`; no oversubscription occurred.

The first C4 failure exposed that challenger attempts were counted inside the normal soft bucket and that emergency checked the unreachable strategy hard bucket. C now counts normal soft usage from primary attempts only and uses the smaller of the configured hard bucket and registered normal soft bucket as the emergency exhaustion boundary.

## C4.2 Rumor, Event Deduplication and Conservative Pro

- A single soft rumor remains `abstain`; its positive dimension stays at 50.
- Duplicate event evidence is routed once and cannot manufacture two-source positive corroboration.
- A confirming challenger cannot increase a primary dimension's score or confidence.
- The same candidate remains blocked by the local `pledge_risk` hard filter and remains `observe` under local downside protection after challenger merge; Pro has no field or path that can relax either guard.

## C4.3 Atomic Physical Counting and Cache Exclusions

- HTTP 429 plus successful retry creates two physical reservations with one failed and one successful terminal row.
- Schema-invalid success plus one successful repair creates two successful physical reservations.
- The same facts reused by today, tomorrow and d25 create one physical reservation; raw-cache hits and local strategy projections create none.
- After 22 today primary attempts, an ordinary candidate remains locally degraded with `budget_exhausted`, while a new high-risk candidate automatically uses one emergency reservation. C4 fixed the reviewer fallback to recognize `soft_bucket_limit` and classifies challenger soft-limit exhaustion as budget exhaustion.

## C4.4 DeepSeek Result Republication

- The fixed in-process fixture carries eight schema-validated V4 reviews through 30 consecutive recommendation patch publications.
- Nearest-rank P95 is asserted at or below the 1-second contract and all rounds completed inside the threshold.
- This is an in-process result-republication gate, not a claim about external model response latency.

## Gate Status

C4.1-C4.4 are complete from the C lease and ready for A4/G4 integration. C has not started C5 and has not modified shared public contracts or pushed the shared branch.
