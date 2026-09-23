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
