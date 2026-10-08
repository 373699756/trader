# Scripts

`scripts/diagnose_runtime.py` is the only public runtime diagnostic command.
Its profile implementations live under `scripts/runtime_diagnostics/` and
must return bounded, sanitized summaries. A new runtime probe is added as a
module and profile there; it is not added as another top-level script.

The remaining top-level scripts are explicit tools with one owner:

| Tool | Class | Side effects and boundary |
| --- | --- | --- |
| `check_refactor_quality.py` | Quality gate | Read-only repository/Ruff gate; no business data. |
| `check_tomorrow_training_memory.py` | Release/research gate | Runs the explicitly requested bounded training evidence check. |
| `convert_baostock_history.py` | Migration | Explicit legacy conversion; never called by runtime or Web. |
| `generate_long_watchlist_asset.py` | Build asset | Deterministically generates the packaged Long watchlist asset. |
| `migrate_runtime_data.py` | Migration | Operates only on an explicit repository-external copy. |
| `repack_baostock_history_archive.py` | Archive maintenance | Explicit build/activate/rollback/finalize operation under the archive lock. |
| `verify_wheel_install.py` | Release gate | Installs and checks a wheel outside the repository. |

Repeated download, training, scoring, or status workflows belong to
`trader-cli` or a business `entrypoints/commands.py`, not to this directory.
Every retained or newly added tool must have a bounded input/output contract,
an explicit network/write declaration, and an entry in this table before it is
used by a gate or documented command.

Research execution belongs to `trader-cli`; the four former standalone research
scripts have been removed. Pass `--config /absolute/path/config/runtime.json`
before the subcommand. Dependency construction and HTTP session lifetime belong
to `bootstrap.py`; the use cases live under `training/application/`.

| Command | Network | Writes and output | Resource boundary |
| --- | --- | --- | --- |
| `research-h1-capability` | Tencent and Eastmoney sample requests | Reads `--h1-runtime-dir`; seals existing H1 formats into explicit external `--artifact-dir`; summary to stdout or external `--output` | One code, two attempts, no retries/redirects/proxy fallback; default 8 s per request, maximum 60 s; deadline from first request at twice that timeout, checked between chunks with finite socket timeouts; 4 MiB per response; two strategies |
| `research-data-qualification` | Same two sample requests | Explicit `--history-root` read-only; summary only; no archive download/write | Same HTTP limits; archive validation and industry audit scan the full existing snapshot |
| `research-industry-audit` | None; `--tushare-access-points` is metadata | Explicit `--history-root` read-only; stdout aggregation or external report; details require external `--output` | One sequential full-snapshot audit; sample count is an eligibility threshold, not a scan cap |
| `research-terminal-holdout` | None | Reads external `--parent-artifact-dir`; writes immutable Tomorrow/D25/conclusion reports into separate external `--output-dir`; summary to stdout or external `--output` | Two strategy evaluations; preserves insufficient-parent closure, hash verification, conflict and repeat-run semantics |

Exit codes: `0` qualified/validated, `1` insufficient evidence, `2` invalid
arguments/evidence or execution/output failure. Execution failures emit a bounded
error report to stdout, including when the requested report path cannot be written;
the failed destination is never retried. Summary paths must be separate from
evidence input and artifact directories. The capability closure produces
only insufficient evidence, so its normal exit code remains `1`. No command
grants production authority. Ordinary CLI help/config checks do not load these
research implementations. The HTTP deadline is checked during transport; socket
timeouts and deadline checks do not guarantee a hard wall-clock limit. Full archive
audits and report I/O have no hard RSS or total offline execution limit.
Use isolated external copies for refactor acceptance, not active data.

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
