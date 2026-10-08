# Scripts

`scripts/diagnose_runtime.py` is the only public runtime diagnostic command.
Its profile implementations live under `scripts/runtime_diagnostics/` and
must return bounded, sanitized summaries. A new runtime probe is added as a
module and profile there; it is not added as another top-level script.

The remaining top-level scripts are explicit tools with one owner:

| Tool | Class | Side effects and boundary |
| --- | --- | --- |
| `audit_historical_industry_facts.py` | Research audit | Read-only supplier/archive audit; emits a report only. |
| `check_refactor_quality.py` | Quality gate | Read-only repository/Ruff gate; no business data. |
| `check_tomorrow_training_memory.py` | Release/research gate | Runs the explicitly requested bounded training evidence check. |
| `convert_baostock_history.py` | Migration | Explicit legacy conversion; never called by runtime or Web. |
| `generate_long_watchlist_asset.py` | Build asset | Deterministically generates the packaged Long watchlist asset. |
| `h1_point_in_time_capability.py` | Research audit | Bounded point-in-time capability audit. |
| `migrate_runtime_data.py` | Migration | Operates only on an explicit repository-external copy. |
| `point_in_time_terminal_holdout.py` | Research audit | Explicit terminal holdout sealing command. |
| `qualify_point_in_time_data.py` | Research audit | Read-only point-in-time data qualification. |
| `repack_baostock_history_archive.py` | Archive maintenance | Explicit build/activate/rollback/finalize operation under the archive lock. |
| `verify_wheel_install.py` | Release gate | Installs and checks a wheel outside the repository. |

Repeated download, training, scoring, or status workflows belong to
`trader-cli` or a business `entrypoints/commands.py`, not to this directory.
Every retained or newly added tool must have a bounded input/output contract,
an explicit network/write declaration, and an entry in this table before it is
used by a gate or documented command.

History SQLite performance is available through the unified read-only diagnostic:

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile history-sqlite --output -
```

It reads monthly SQLite headers, samples page classes, benchmarks latest-row
queries, and writes a bounded revision fixture only to a disposable temporary
database. It does not compact or switch the active history database. Maintenance rules
are in [the architecture contract](../docs/项目重构详细.md), section 3.5.1.1;
training performance is explained in [the training guide](../docs/04_策略回溯.md),
section 12.4. The repack tool remains a separate explicit maintenance operation;
its default paths do not authorize active-data changes. During refactoring,
rehearse only on explicit repository-external copies as required by section 10.5
of the architecture contract.

Test commands and fast/full boundaries are defined by Makefile and documented
in the architecture contract, section 10.2.1. Default `make test` runs the fast
set; release acceptance also requires `make test-full`. Work status and pending
optimizations are recorded only in `docs/03_工程实施.md`.
