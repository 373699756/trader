# Delivery Record: Close Freeze Contract Calibration

## User request

Continue the remaining engineering tasks, including the freeze-test calibration
identified in stages 47 and 51.

## Cause

Confirmed against pushed baseline `71364ca3`: two targeted checks failed. The
draft-visibility regression still treated 14:50 as the freeze boundary, although
the authoritative contract and current implementation use 15:00. The document
check imposed a 900-line cap without contractual support after required
data-source contracts were merged into the scoring authority.

Regression-Key: `close-freeze-read-model-contract-calibration`.

## Behavior change

- Calibrate draft visibility for Tomorrow and D25 at 09:25, 14:40, 14:50,
  14:59:59, 15:00, 15:00:01 and 15:05. Before close the observation draft remains
  visible; at and after close it cannot substitute for a missing formal record.
- Extend real temporary SQLite coverage through scheduled freeze, reopened
  repositories, checkpoint recovery, formal-record restoration and current/history
  read models. Cover both strategies, hot reads and cold restoration.
- Exercise permitted current/local-rebuild close fallback at and after close,
  early/unofficial-close rejection, first-wins and late-result protection. Retain
  existing empty-formal, strategy-isolation and pending-seal regressions.
- Remove only the arbitrary document size assertion; preserve chapter ordering,
  semantic invariants and implementation-noise checks. No production code,
  formula, configuration, freeze policy or public schema changes.

## Verification

Before calibration, the two historical checks failed. Final targeted gate:
decision queries, freezing, document chain, unified decision core, streams,
identity, SQLite records, native projection and authority/document/Changelog/Web
contracts: 150 passed. The three changed Python test files pass Ruff and format
checks. Complete baseline diff review and `git diff --check` passed.

New fixture development initially exposed invalid future quote timestamps,
incorrect record-versus-decision identity assertions and omitted CAS versions;
these fixture mistakes were corrected without weakening production validation.

## Live evidence and residual risks

The time matrix uses injected Shanghai clocks and real temporary SQLite files;
reopened repository/index instances model recovery without claiming an actual
service restart or browser acceptance. No active data was accessed, no service
was restarted, and no supplier or DeepSeek quota was spent. Live service,
supplier, browser and freeze/undo recovery acceptance were not run in this batch.

Independent complete static population, real stage 5–9 orchestration and removal
of synthetic latency/health, all-attempted-source health, long-term history
coverage and return research remain for separate batches. The user's scoring
ratio TODO is preserved outside this commit.
