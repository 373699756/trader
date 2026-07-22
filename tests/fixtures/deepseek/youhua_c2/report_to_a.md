# Youhua C2 Report to A

```text
codex_and_phase: Codex C / C2.1-C2.8
base_commit: 45bd2fab992d36eb873b7c448fbd9739f0cad43c
head_commit_or_patch: patch only; no C-owned commit or push in shared worktree
owned_paths_changed:
  - src/trader/infra/deepseek/budget.py
  - src/trader/infra/deepseek/evidence_router.py
  - src/trader/infra/deepseek/reviewer.py
  - src/trader/infra/deepseek/reviewer_requests.py
  - src/trader/infra/deepseek/schema.py
  - tests/component/test_v2_deepseek.py
  - tests/component/test_youhua_deepseek_c2.py
contract_assumptions:
  - CONTRACT_BASE=45bd2fab992d36eb873b7c448fbd9739f0cad43c from docs/reports/youhua-g1-contract-base.md
  - DeepSeek V4 facts schema owner remains A; C implements parser/prompt/cache/budget behavior only
  - Public ports, pipeline routing, runtime config, CHANGELOG and public docs remain A-owned
schema_or_migration_changes:
  - SCHEMA_VERSION=deepseek_v4_review_facts_v1
  - PROMPT_VERSION=deepseek_v4_review_facts_prompt_v1
  - Legacy deepseek_review_v3 response parsing remains accepted for old frozen/replay compatibility
tests_run_and_results:
  - pytest C DeepSeek unit/component scope: passed
  - ruff C DeepSeek source/test scope: passed
  - mypy src/trader/infra/deepseek src/trader/domain/review: passed
  - git diff --check: passed
performance_before_after:
  - baseline_peak_rss_bytes=35622912
  - flash_8_peak_rss_bytes=36933632
  - pro_4_peak_rss_bytes=37064704
  - repair_peak_rss_bytes=37064704
known_failures_and_risks:
  - Shared worktree contains A/B/D uncommitted changes; C only modified C lease files.
  - Public config and application caller alignment remain A-owned.
requested_interface_changes:
  - none
ready_for_gate: yes
```

## Scope

- Codex: C
- Phase: C2.1-C2.8
- Contract base: 45bd2fab992d36eb873b7c448fbd9739f0cad43c
- Source gate: `docs/reports/youhua-g1-contract-base.md`
- Boundary: only DeepSeek V4 facts, routing, budget and cache/review tests in C lease
- Out of scope: Polars, P6/Web, public API wiring, changelog, shared contracts

## Implemented

- C2.1: `long` DeepSeek review is permanently empty and does not reserve budget, call HTTP, reuse raw cache or create budget rows.
- C2.2: primary prompt and parser use `deepseek_v4_review_facts_v1` with fact-only fields: catalyst, price reaction, fundamentals, industry policy, risks, conflicts and coverage.
- C2.3: prompt evidence carries `source_tier` and `event_key`; routing deduplicates same events and caps each stock at 12 prompt evidence items.
- C2.4: V4 local projection starts dimensions at 50, clamps mapped dimensions to 30-70, applies positive uplift only for official or two independent trusted evidence, and leaves DeepSeek risk facts as penalty-free/veto-free facts.
- C2.5: review eligibility keeps high-value/edge cases inside existing reviewer priority path; public caller changes remain A-owned.
- C2.6: hard rejects `long`; soft limits are shared preheat 10, today 22, tomorrow 14, d25 12, Pro/challenger 8 and emergency 5, while physical attempts still count through existing budget accounting.
- C2.7: Flash primary batch remains max 8, Pro challenger batch is max 4, prompt evidence is max 12 per stock, and raw review cache identity includes V4 schema/prompt plus routed evidence manifest.
- C2.8: added regression coverage for long isolation, V4 field rejection, evidence quality, risk fact separation, evidence cap/dedupe and soft budget limits.

## Changed Files

- `src/trader/infra/deepseek/budget.py`
- `src/trader/infra/deepseek/evidence_router.py`
- `src/trader/infra/deepseek/reviewer.py`
- `src/trader/infra/deepseek/reviewer_requests.py`
- `src/trader/infra/deepseek/schema.py`
- `tests/component/test_v2_deepseek.py`
- `tests/component/test_youhua_deepseek_c2.py`

## Verification

- `.venv/bin/python -m pytest tests/unit/test_v2_deepseek_base.py tests/component/test_v2_deepseek.py tests/component/test_v2_deepseek_v4.py tests/component/test_youhua_deepseek_c2.py -q` passed.
- `.venv/bin/python -m ruff check src/trader/infra/deepseek/schema.py src/trader/infra/deepseek/evidence_router.py src/trader/infra/deepseek/budget.py src/trader/infra/deepseek/reviewer.py src/trader/infra/deepseek/reviewer_requests.py tests/component/test_v2_deepseek.py tests/component/test_youhua_deepseek_c2.py` passed.
- `.venv/bin/python -m mypy src/trader/infra/deepseek/schema.py src/trader/infra/deepseek/evidence_router.py src/trader/infra/deepseek/budget.py src/trader/infra/deepseek/reviewer.py src/trader/infra/deepseek/reviewer_requests.py` passed.
- `.venv/bin/python -m mypy src/trader/infra/deepseek src/trader/domain/review` passed.
- `git diff --check` passed for current worktree.
- Peak RSS probe with fake DeepSeek client: baseline 35,622,912 bytes; Flash max 8 36,933,632 bytes; Pro max 4 37,064,704 bytes; repair 37,064,704 bytes.

## Residual Risks

- Shared worktree contains A/B/D uncommitted changes; C only modified the C lease files listed above.
- Public config and application caller alignment for removing long budget/config exposure remain A-owned; C enforces long isolation inside the DeepSeek reviewer and budget store.
- C did not update `CHANGELOG.md`, public docs, public routes, P6/Web, Polars or shared wiring by role constraint.

## Gate Status

- C2 ready for G2 review from C side.
- C is waiting for the shared G2 gate before starting C3.x.
