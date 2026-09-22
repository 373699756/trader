# Stack dashboard Top scores vertically

## User request

Remove the duplicated highest-score value from the data-status funnel counts and display the highest-scoring
stocks vertically so their code and name remain visible.

Regression-Key: `dashboard-top-scores-vertical-layout-v1`

## Cause and current-state judgment

The score range already included the highest value, while the final-count line repeated it. The Top-score renderer
joined stocks into one line and its CSS fixed the container to one 17px row with no wrapping, clipping later stocks.
The API data, score ordering and selection semantics were already correct.

## Changed

- The final-count line no longer repeats the highest score; the score range remains its single visible owner.
- Top-score stocks are separated by line breaks and the data-status grid gives the list its natural multi-line height.
- The score range and metadata remain on the left, while the Top-score list remains on the right without ellipsis.
- The authoritative Web contract and current engineering description now record the non-duplicated, vertical layout.

## Verification

- JavaScript syntax and dashboard-state contracts passed.
- Web app-factory and HTTP/Web contracts passed with 11 tests; affected Ruff checks passed.
- The browser refresh diagnostic passed with 42ms patch-to-paint P95 against the 100ms budget.
- Isolated Firefox acceptance passed at 1280x720, 1440x900 and 1920x1080 with no page overflow or browser errors;
  the Top-score element rendered two fixture stocks at two full 17px rows.
- `git diff --check` passed.

## Residual risks

- Browser evidence uses the repository fixture rather than live supplier data. No API schema, score calculation,
  ranking, runtime state or activity data changed.
