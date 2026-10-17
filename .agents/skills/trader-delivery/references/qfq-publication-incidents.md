# qfq paired-publication incidents

Use this playbook when a qfq run reports successful supplier rows but leaves pending codes, when V2/V3 membership differs, or when the active databases and `.checkpoint.json` disagree. Complete the checkpoints in order and classify each as confirmed, ruled out, or unavailable. Do not stop after proving that Tencent returned data.

## 1. `supplier-download`

- Separate request success from a durable update. Record the bounded run's requested, successful, failed and cancelled counts plus its target date and source identity class.
- A successful raw/qfq response proves only that bytes reached validation. It does not prove pair validation, SQLite commit, checkpoint confirmation or recommendation visibility.
- Use `tencent-download` for a bounded disposable updater probe. Do not spend a full-market request wave merely to classify a persistence failure.

## 2. `pair-validation`

- Confirm that raw and qfq rows share the required trading dates and pass the bounded window/content rules.
- Keep missing supplier days, invalid payloads, source changes and overlap conflicts distinct from persistence failures.
- If validation never produced a publishable window, `qfq_pair_publication_failed` is not the first broken boundary.

## 3. `paired-publication`

- Confirm whether both profile files committed in the same attached SQLite transaction. Inspect the exception cause, committed board set, changed-file set and changed-row count without exposing stock payloads.
- Run `.venv/bin/python3 scripts/diagnose_runtime.py --profile qfq-sqlite --output -` for the active-data read-only audit. It must not open a long SQLite read transaction on an active shard: copy each shard without a SQLite lock, reject changing file/journal identity, and run `quick_check` on the disposable copy. Record integrity, journal modes, hot-journal counts, V2/V3 membership and source/cutoff/calendar mismatch counts.
- `qfq_pair_publication_failed` is a generic publication category. `qfq_pair_database_busy` specifically identifies SQLite locked/busy contention; preserve the chained SQLite exception for local diagnosis.

## 4. `checkpoint-confirmation`

- A code enters `.checkpoint.json` only after both profiles commit. Compare aggregate `paired_codes`, `checkpoint_codes`, `database_not_checkpointed` and `checkpoint_not_paired` counts.
- `database_not_checkpointed > 0` means durable paired rows exist without confirmation and can be safely reconsidered by the updater. `checkpoint_not_paired > 0` is more severe because the progress identity claims completion without a current pair.
- Bind the comparison to checkpoint day and source. Do not infer success from equal total counts alone.

## 5. `reader-writer-contention`

- Identify every active reader of the affected shard and the exact duration of its SQLite transaction. Decode, feature construction, network work and sleeps must occur after the read transaction releases its shared lock.
- Reproduce contention deterministically on disposable databases. Hold the reader's post-fetch work while requiring the paired writer to finish inside its timeout, then prove a manifest change rejects the mixed projection and a later refresh accepts the new identity.
- Comparing a retry after stopping the service is useful evidence of contention, but stopping the service is not the repair. The regression must pass while the reader and writer overlap.

## Closure evidence

Close the incident only after preserving the original failure shape, identifying the first broken checkpoint, passing the paired writer/reader regression, and running the active read-only audit. Record any remaining real 15:10 service-window verification separately; fixture success does not prove that scheduled maintenance loaded the new release.
