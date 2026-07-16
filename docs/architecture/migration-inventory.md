# v1 to v2 Migration Inventory

Status: implementation cutover complete; production release pending
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
