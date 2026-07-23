# Youhua C5 Final Sign-off to A

```text
codex_and_phase: Codex C / C5.1-C5.2
status: PASS
base_commit: e179358458429b27c6afc71bf80dc961575fe242
head_commit_or_patch: patch only; C5 changes remain uncommitted in the shared worktree and C did not push
owned_paths_changed:
  - src/trader/infra/deepseek/cache.py
  - src/trader/infra/deepseek/challenger.py
  - src/trader/infra/deepseek/evidence_router.py
  - src/trader/infra/deepseek/reviewer.py
  - src/trader/infra/deepseek/reviewer_requests.py
  - src/trader/infra/deepseek/schema.py
  - tests/component/test_v2_deepseek_v4.py
  - tests/component/test_youhua_deepseek_c2.py
  - tests/component/test_youhua_deepseek_c3.py
  - tests/component/test_youhua_deepseek_c4.py
  - tests/fixtures/deepseek/youhua_c5/report_to_a.md
contract_assumptions:
  - docs/reports/youhua-g4-gate-review.md formally publishes G4 before C5 started.
  - Authoritative DeepSeek thresholds are at least two known dimensions and strategy-weighted coverage >= 0.50.
  - Public ports, configuration, pipeline wiring, authoritative documents, CHANGELOG and Git operations remain A-owned.
schema_or_migration_changes:
  - No public schema version or SQLite migration changed.
  - Existing deepseek_v4_review_facts_v1 and deepseek_challenger_v1 parsing is stricter for unknown fields and text types.
  - Legacy deepseek_review_v3 parsing remains available for frozen/replay compatibility.
tests_run_and_results:
  - DeepSeek unit/component scope: 116 passed; 9 known non-failing fixture-model warnings.
  - Recommendation/fusion/downside/risk scope: 40 passed.
  - Ruff format: 235 files already formatted.
  - Ruff E/F/I/B/UP: passed.
  - Strict refactor debt: passed at C901=36, N818=5, PLR0911=15, PLR0912=15, PLR0913=55, PLR0915=11.
  - mypy src/trader: passed, 164 source files.
  - C-owned git diff --check: passed.
  - make lint: passed.
  - make test: passed after A's final-review report became available in the shared worktree.
  - make package: passed after isolated-build dependency access was allowed.
  - Outside-repository wheel verification: imported trader from the external target, executed CLI help, read all
    9 template/CSS/JavaScript/SVG resources and passed pip check using the existing locked runtime dependencies.
performance_before_after:
  - No new physical-request path, cache pool or unbounded collection was added.
  - C4/G4 request-count, atomic-budget and in-process republication evidence remains applicable after C5 regression.
known_failures_and_risks:
  - Real DeepSeek vendor latency and availability remain external runtime risks; local fallback behavior is covered.
  - Tests using fixture model name "model" retain the known non-failing model-catalog warning.
  - A still owns the final A5 commit/push and G5 hash verification; C's revalidation does not authorize or replace it.
requested_interface_changes:
  - none
ready_for_gate: yes
```

## C5.1 Final DeepSeek Diff Review

The final DeepSeek implementation and the C2-C4 reports agree on the production boundaries:

- `long` cannot reserve primary, challenger, shared-preheat or emergency budget and performs zero physical HTTP calls.
- Flash accepts fact-only V4 responses; the parser rejects decision fields, unknown root/nested fields, invalid result
  code types, oversized responses and evidence references outside the routed manifest.
- Challenger responses are bounded and strict at the root, result, dimensions container and individual-dimension levels.
  A challenger can only preserve or reduce a primary result; it cannot raise score/confidence or relax local guards.
- Positive catalyst, price-reaction, fundamental and policy uplifts require the configured trusted evidence quality;
  positive catalyst/price mappings additionally require confirmed facts. Soft, unconfirmed and conflicting material
  cannot add points.
- V4 facts are first mapped locally to bounded dimensions. Strategy-specific application is decided only by the
  authoritative two-dimension and weighted-coverage `0.50` classifier, avoiding a second hard-coded threshold.
- Evidence routing enforces point-in-time timestamps, normalizes cross-timezone event keys, keeps the higher-quality
  and earlier-received duplicate, rejects invalid evidence IDs and caps routed evidence at 12 per stock.
- The raw facts prompt/cache identity contains only common structured-review inputs, routed evidence (including
  receipt time), risk facts and model/schema identity. Strategy policy, strategy-only scores and merge epochs no longer
  split today/tomorrow/d25 raw requests; material facts/evidence changes still invalidate the cache.
- Emergency eligibility is scoped to each physical candidate batch, so a high-risk batch cannot leak emergency access
  to a later ordinary batch. Existing soft/hard/global atomic accounting remains unchanged.

The review covered correctness, conservative risk behavior, deadlines, retry/repair counting, cache identity,
boundedness, concurrency ownership, legacy replay compatibility, typing, architecture line limits and installability
impact. No unresolved C-owned finding remains.

## C5.2 Evidence and Residual Risk

The final C regression includes the 58 normal, 66 normal-plus-Pro, 71 emergency-inclusive and 188 global hard-limit
vectors; cross-strategy raw-facts reuse; quote-tolerance reuse; evidence-change invalidation; long zero-request
behavior; schema repair/retry counting; rumor and duplicate-event conservatism; and local fallback on complete model
failure. Recommendation fusion/risk regressions remain green, including the fixed `83.40` contract exercised by the
existing domain suite.

After D5 was committed and pushed as `e179358458429b27c6afc71bf80dc961575fe242`, C repeated the complete C
regression and the repository format, lint, type, test and package gates without finding a C/D integration
regression. The outside-repository wheel also imported from its external installation target, exposed the CLI and
all nine packaged Web resources, and passed dependency checking with the existing locked runtime dependencies.

The current checkout is a shared integration worktree. C deliberately did not edit A/D-owned application, Web,
contract-document, CHANGELOG or final-delivery files and did not stage, commit or push. A must include this report and
the listed C-owned patch in its single A5 delivery commit, rerun the final repository/package/wheel gates, and publish
G5 only after all sign-offs and `HEAD == @{upstream}` are confirmed.

## Verdict

PASS. Codex C signs off C5.1-C5.2 with `ready_for_gate: yes`; no public interface change is requested.
