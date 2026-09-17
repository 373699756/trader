# Remove Public History Automation Installation Commands

## User Request

Remove the public `run.sh`/PowerShell installation and uninstallation commands while keeping the history download, training, recommendation, and scheduled maintenance responsibilities independent.

## Current State And Cause

The system-task renderer and scheduled runner are still needed by already-installed user tasks, but exposing installation and removal as launcher commands made task maintenance part of the normal research entrypoint. That coupled task registration with the public download/training command surface and left stale commands in the CLI and documentation contracts.

## Added

None. This batch removed a duplicated public surface and introduced no new command, module, or configuration key.

## Changed

- Removed `install-history-automation` and `uninstall-history-automation` from the Python CLI parser and dispatch.
- Removed both commands from `run.sh` and `run.ps1` help, mode allow-lists, and forwarding paths.
- Kept `scheduled-history-maintenance` as an internal zero-argument runner for existing tasks.
- Kept `history-automation-status` as a read-only status command.
- Updated active README and engineering command tables to describe the new boundary.
- Added contract coverage proving retired commands fail before environment setup and are absent from PowerShell public modes.

## Fixed

None. The retired commands were removed rather than corrected, and the retained scheduled runner keeps its existing behavior.

## Removed

The public installation and uninstallation entrypoints are no longer supported. The underlying platform templates and installation implementation remain internal for now so existing installations are not disrupted; removing that implementation requires a separate migration batch.

## Verification

- `pytest -q tests/contract/test_history_automation_contract.py tests/contract/test_entry_contract.py tests/contract/test_current_product_contract.py tests/contract/test_strategy_replay_document_contract.py` passed.
- Ruff, mypy, `git diff --check`, and the final review are run before delivery.

## Residual Risks

Existing user-level timers or launch agents are not automatically deleted. They continue to invoke the retained scheduled runner until an explicit operational migration removes them. New users cannot register or unregister tasks through the public `run.sh`/PowerShell surface.

Regression-Key: history-automation-entrypoint-cleanup
