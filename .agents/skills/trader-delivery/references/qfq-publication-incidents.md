# qfq paired-publication incidents

Use this playbook when a qfq run reports successful supplier rows but leaves pending codes, when V2/V3 membership differs, or when identities and `completions` in the shared daily-window SQLite database disagree. Physical layout and transaction authority remain in `docs/4_下载训练.md` section 4. Complete the checkpoints in order and classify each as confirmed, ruled out, or unavailable. Do not stop after proving that Tencent returned data.

## 1. `supplier-download`

- Separate request success from a durable update. Record the bounded run's requested, successful, failed and cancelled counts plus its target date and source identity class.
- A successful raw/qfq response proves only that bytes reached validation. It does not prove pair validation, SQLite commit, checkpoint confirmation or recommendation visibility.
- Use `tencent-download` for a bounded disposable updater probe. Do not spend a full-market request wave merely to classify a persistence failure.

## 2. `pair-validation`

- Confirm that raw and qfq rows share the required trading dates and pass the bounded window/content rules.
- Keep missing supplier days, invalid payloads, source changes and overlap conflicts distinct from persistence failures.
- If validation never produced a publishable window, `qfq_pair_publication_failed` is not the first broken boundary.

## 3. `paired-publication`

- Confirm whether prices, both profile identities, calendars, completion and publication rows committed in one shared-database transaction. Inspect the exception cause and committed row changes without exposing stock payloads.
- Run `.venv/bin/python3 scripts/diagnose_runtime.py --profile qfq-sqlite --output -` for the read-only shared-database audit. Record integrity, WAL mode, profile counts, paired-identity mismatches, completion differences and `publication_changed_during_audit`. The audit reads active data and does not prove that it causes no contention; large read-cost experiments belong on an isolated copy.
- `qfq_pair_publication_failed` is a generic publication category. `qfq_pair_database_busy` specifically identifies SQLite locked/busy contention; preserve the chained SQLite exception for local diagnosis.

## 4. `checkpoint-confirmation`

- A code is confirmed by the SQLite `completions` row in the paired publication transaction. Compare aggregate `paired_identity_mismatches`, `database_not_checkpointed` and `checkpoint_not_paired` from the current diagnostic; the last two public field names describe completion-row differences, not a separate checkpoint file.
- `database_not_checkpointed > 0` means durable paired rows exist without confirmation and can be safely reconsidered by the updater. `checkpoint_not_paired > 0` is more severe because the progress identity claims completion without a current pair.
- Bind the comparison to checkpoint day and source. Do not infer success from equal total counts alone.

## 5. `reader-writer-contention`

- Identify every active reader of the shared database and the exact duration of its SQLite transaction. Decode, feature construction, network work and sleeps belong outside that transaction; do not infer lock behavior from the retired shard layout.
- Reproduce contention deterministically on disposable databases with the current WAL configuration. Hold the reader's post-fetch work while requiring the paired writer to finish inside its timeout, then prove a publication change rejects a mixed startup projection and a new bounded read accepts the new identity.
- Comparing a retry after stopping the service is useful evidence of contention, but stopping the service is not the repair. The regression must pass while the reader and writer overlap.

## Closure evidence

Close the incident only after preserving the original failure shape, identifying the first broken checkpoint, passing the paired writer/reader regression, and running the active read-only audit. Record any remaining real target-date or startup-read verification separately; fixture success does not prove that manual maintenance or the running service loaded the new release.
