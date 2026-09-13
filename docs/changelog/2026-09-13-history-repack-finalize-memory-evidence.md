# History repack finalize memory-evidence compatibility

## User Request

Fix the `history_repack_document_fields_are_invalid` failure reported by
`scripts/repack_baostock_history_archive.py finalize`, then commit and push the
repair without deleting the retained pre-repack history backup.

Regression-Key: `history-repack-finalize-memory-evidence-schema-drift`

## Cause

The training-memory check had started emitting `sample_database_peak_bytes`
and `stage_durations_ms`, but the finalize boundary still required the older
exact field set. The evidence content hash and the measured training result
were valid; decoding failed before finalize could verify or release the backup.

## Added

- Typed per-stage training-duration evidence with finite, non-negative values
  and an explicit training-stage allowlist.
- A regression fixture that writes and decodes the current signed
  training-memory document before exercising finalize.

## Changed

- The finalize evidence decoder now accepts the two current measurement fields
  and preserves them as typed values.
- Starting RSS evidence is validated against the measured peak while the exact
  JSON field whitelist and content-hash verification remain fail-closed.

## Fixed

- Current `training-memory-result.json` documents no longer fail solely because
  they contain the database-size and stage-duration measurements produced by
  the memory-check command.

## Removed

- The successful-finalize unit path no longer mocks the evidence decoder, so
  producer/consumer field drift is covered by the regression test.

## Verification

- Red/green regression: the current-format finalize fixture failed with
  `history repack document fields are invalid` before the decoder change and
  passed after it.
- The real retained `data/historyless/training-memory-result.json` decoded
  successfully with training status `engineering_ready`, repeat status
  `already_current`, database peak `1446711296` bytes, and six stage timings.
- Direct regression and adjacent contracts passed:
  `test_history_archive_repack.py`, `test_check_tomorrow_training_memory.py`,
  `test_history_archive_repack_contract.py`, and
  `test_professional_naming_contract.py`.
- Full quality gates passed: `make format-check`, `make lint`,
  `make type-check`, `make test`, and `make package`.

## Residual Risks

- Finalize was intentionally not rerun during this code change because it
  irreversibly removes the verified 24 GiB backup. The backup remains available
  until the operator explicitly reruns the command after installing this fix.
- Runtime migration data under `data/historyless/` and unrelated local
  `docs/v1v2.md` content are excluded from this delivery.
