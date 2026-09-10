# Delivery Record: Changelog Archive

## User Request

Reduce the token and review cost of the oversized root `CHANGELOG.md` without
losing the repository's delivery evidence.

## Cause

Confirmed: completed delivery batches were appended indefinitely beneath one
root `Unreleased` section. The root file reached 9,694 lines and 1,050,930
bytes, so routine changelog lookup and editing repeatedly loaded unrelated
historical evidence.

## Added

- Added a bounded root index, a searchable delivery-record directory, and an
  explicit archive contract test.
- Added this delivery record and a lightweight index for future batches.
- Added a path-specific Git whitespace exception so the verbatim legacy
  snapshot remains auditable without masking whitespace checks elsewhere.

## Changed

- Moved the pre-existing root changelog verbatim to the immutable legacy
  snapshot. The root now points only to the latest delivery record and index.
- Updated delivery governance, authoritative documentation, and historical
  evidence tests to read the delivery-record collection rather than require
  historical evidence in the root file.

## Fixed

- Restored the intended meaning of `Unreleased` as a bounded latest-batch
  pointer instead of an unbounded history container.

## Removed

- Removed the duplicated historical payload from the root changelog; no
  delivery evidence was deleted.

## Verification

- The seven affected document-contract modules passed, including the new root
  size, archive-link, legacy-evidence, and required-section checks.
- The legacy snapshot SHA-256 exactly matches the pre-migration root content:
  `e206e032834e71666bd82ad7077210d84a653dc7c7a5e284d46dcdcc954b396f`.
- The root entry is 307 bytes, down from 1,050,930 bytes; full runtime,
  package, and browser gates are not applicable to this documentation-only
  batch.
- `git diff --cached --check`, Ruff lint, and Ruff format checks passed for
  every changed Python contract test.

## Residual Risks

- This change does not validate product runtime behavior because it changes
  only repository documentation and document-contract tests.
