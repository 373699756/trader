# Youhua C3 Report to A

```text
codex_and_phase: Codex C / C3.1-C3.4
base_commit: 812de4390d42213c684b4e2096453c4775903443
head_commit_or_patch: patch only; no C-owned commit or push in shared worktree
owned_paths_changed:
  - src/trader/infra/deepseek/reviewer.py
  - src/trader/infra/deepseek/reviewer_requests.py
  - src/trader/infra/deepseek/schema.py
  - tests/component/test_youhua_deepseek_c3.py
  - tests/fixtures/deepseek/youhua_c3/report_to_a.md
contract_assumptions:
  - docs/reports/youhua-a3-integration.md declares A3 integration handoff available.
  - C3 verifies DeepSeek integration behavior only; public wiring, Polars and P6/Web remain outside C scope.
schema_or_migration_changes:
  - Added internal RAW_FACTS_CACHE_GENERATION=raw_facts_v1 for strategy/phase-independent raw facts cache identity.
  - Public DeepSeek facts schema remains deepseek_v4_review_facts_v1.
tests_run_and_results:
  - .venv/bin/python -m pytest tests/component/test_youhua_deepseek_c3.py -q: passed
  - .venv/bin/python -m pytest tests/unit/test_v2_deepseek_base.py tests/component/test_v2_deepseek.py tests/component/test_v2_deepseek_v4.py tests/component/test_youhua_deepseek_c2.py tests/component/test_youhua_deepseek_c3.py -q: passed
  - .venv/bin/python -m ruff check src/trader/infra/deepseek/schema.py src/trader/infra/deepseek/reviewer.py src/trader/infra/deepseek/reviewer_requests.py tests/component/test_youhua_deepseek_c3.py: passed
  - .venv/bin/python -m mypy src/trader/infra/deepseek src/trader/domain/review tests/component/test_youhua_deepseek_c3.py: passed
  - git diff --check: passed
performance_before_after:
  - not applicable; C3 covered request-count and degradation behavior, not a new latency/RSS budget.
known_failures_and_risks:
  - Existing test_v2_deepseek warnings for fixture model name "model" remain non-failing existing warnings.
  - Full G3 publication still waits for B3 and D3 ready reports plus A's shared gate.
requested_interface_changes:
  - none
ready_for_gate: yes
```

## C3.1 Cross-Strategy Raw Facts

Verified the same stock across today, tomorrow and d25 produces one physical V4 raw facts HTTP request and reuses the
raw facts cache for the other strategy projections. The first C3 run exposed a phase-scoped raw cache identity that
caused today and afternoon strategies to duplicate physical requests. C fixed this inside DeepSeek infra by using
`RAW_FACTS_CACHE_GENERATION=raw_facts_v1` for primary raw facts cache keys; strategy-specific local projection remains
separate through existing fusion cache keys.

## C3.2 Price Change Routing

Verified quote-only changes inside the existing quote-change tolerance hit raw cache and add no physical HTTP request.
Verified evidence manifest change changes the raw facts identity and routes to a new physical review request.

## C3.3 Long, Window, Budget and Failure Degradation

Verified long review remains empty with zero budget and zero HTTP in the A3 integration state. Verified late requests
return `LATE` without budget rows, hard-budget exhaustion returns a rejected review without an extra HTTP call, and
all-fail HTTP retry paths return rejected reviews while atomically counting both physical attempts.

## C3.4 C-Owned Fixes

No A-assigned C-owned integration bug was present before running C3. C3.1 found and fixed one C-owned cache identity
issue during C verification. No public interface change is requested.
