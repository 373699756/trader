# v1 to v2 Migration Inventory

Status: repository cutover gates complete; external full-trading-day observation pending
Contract: `docs/need.md` v6

## Baseline

The active v1 tree contains 132 Python modules, including 82 modules directly
under `stock_analyzer/`, plus 59 test modules. The largest modules combine
persistence, application orchestration, validation, and Web behavior. The v2
rewrite does not preserve that module layout.

## Disposition

| v1 area | Disposition | v2 owner |
| --- | --- | --- |
| `scoring_core/`, `strategies/`, normalization and risk rules | rewrite | `src/trader/domain` |
| `runtime.py`, schedulers, background workers and application services | rewrite by use case | `src/trader/application` |
| quote, history, fundamental, calendar and news providers | rewrite by provider | `src/trader/infrastructure/market_data` |
| production DeepSeek HTTP, schema, cache and budget behavior | rewrite | `src/trader/infrastructure/deepseek` |
| SQLite, JSON snapshots and cache files | rewrite | `src/trader/infrastructure/persistence` |
| Flask app, recommendation routes, templates and recommendation assets | rewrite | `src/trader/web` |
| process entry, server lock and supported maintenance commands | rewrite | `src/trader/entrypoints` |
| validation, backtest, calibration, prediction, paper trading, OOS, experiment and tuning modules | delete after cutover | Git history and `docs/archive/v1` |
| validation/prediction Web routes and assets | delete after cutover | none |
| v1 runtime data | preserve read-only | `.runtime/` |
| v2 runtime data | new isolated store | `.runtime/v2/` |

## Migration Rules

1. `src/trader` must never import `stock_analyzer`.
2. A v1 module is not moved wholesale when it contains more than one boundary.
3. Retained behavior is captured with v2 tests and deterministic fixtures.
4. Deleted behavior is removed with its routes, assets, configuration and tests.
5. The default launcher changes only after the installed v2 package passes its
   unit, contract, integration, build and smoke gates.
6. Rollback uses the complete v1 release and untouched v1 runtime data; v2 does
   not write into a v1 database or snapshot.

## Implementation Cutover Result

- Active product code exists only in `src/trader`; the v1 package, root app,
  root Web resources, duplicate requirements and deleted-feature tests are gone.
- Historical v1 requirements, plans, design notes, experiment registry and
  configuration are retained under `docs/archive/v1` and are not executable.
- The wheel contains templates, CSS, JavaScript and the dashboard icon.
- `trader-server` and `trader-cli` run outside the repository root with an
  explicit configuration path.
- Recovery tests verify staged/committed manifests, hash validation and frozen
  snapshot immutability.

## Section 24 Completion Evidence

| Need section | Repository evidence | Status |
| --- | --- | --- |
| 24.1-24.2 mapping and frozen contracts | This inventory gives every legacy area one `rewrite`, `delete`, `archive`, `preserve read-only` or isolated-v2 disposition; configuration and fixed factor/fusion contracts live in `config/v2` and their contract tests. | complete |
| 24.3 engineering skeleton | `src/trader`, dependency-direction/app-factory contracts, package data, console scripts, Make targets and repository-external wheel smoke acceptance. | complete |
| 24.4 domain core | Domain unit tests cover filters, factors, all four strategies, risk, stable TopK and the fixed `83.40` fusion vector without I/O imports. | complete |
| 24.5 adapters and persistence | Component tests cover calendar fail-closed behavior, quote normalization/fallback/health, direct transport, feature completeness, SQLite/JSON manifests, hash recovery and immutable freezes. | complete |
| 24.6 pipeline and freeze | Integration/unit tests cover the virtual trading timeline, bounded priority/coalescing queues, persisted freeze events, replay, restart gates and long non-freeze behavior. | complete |
| 24.7 DeepSeek | Mock component tests cover schema/evidence rejection, physical retry accounting, timeout, deadline, shared cache and atomic 188-call buckets; missing real credentials remains an external smoke risk. | complete with external risk |
| 24.8 Web and installation | Contract tests cover read-only API, ETag, SSE cursor/resync/limits, lazy app creation and package assets; desktop browser sizes remain part of release acceptance. | complete with external risk |
| 24.9 cutover and rollback | `test_recorded_full_day_shadow_is_deterministic_and_freezes_real_repository` runs the full recorded timeline twice against real SQLite/JSON persistence and compares every JSON hash. Rollback tag `v1-rollback-20260717` identifies commit `86e3b2b1308e454adee1e1cc43fa0c8997e8bf2b`. | repository gate complete |

The recorded shadow is deterministic release evidence, not a substitute for a
live A-share day. Production release remains pending until one uninterrupted
09:15-15:00 run records successful today/tomorrow/d25 freezes, source-age
metrics and desktop observations without writing to the v1 runtime.
