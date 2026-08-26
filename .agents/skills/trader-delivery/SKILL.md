---
name: trader-delivery
description: Plan, implement, diagnose, review, and verify changes in the Trader A-share dashboard when work can affect market data, history warmup, scoring, DeepSeek, scheduling, freezing, persistence, API/SSE, Web, runtime performance, or release behavior. Use for repository change plans, bug fixes, performance work, and engineering reviews; do not use for a purely explanatory read-only question with no requested repository change.
---

# Trader Delivery

Deliver one repository change without reopening a known failure or regressing an adjacent boundary.

## Start the batch

1. Follow the repository `AGENTS.md`; this skill adds routing and evidence requirements but does not duplicate or override it.
2. Record `HEAD`, `@{upstream}`, staged and unstaged files, and the exact task file scope before editing. Preserve unrelated user changes.
3. Search `CHANGELOG.md` for the reported symptom, error code, affected strategy, and likely boundary. Treat old root causes as leads, not current facts.
4. Read the applicable authoritative contract before planning: `docs/software-business-design.md` for architecture, runtime, API, Web, operations, and acceptance; `docs/recommendation-strategy.md` for candidate, score, risk, DeepSeek, fusion, action, and ranking behavior.
5. Read [the change-impact matrix](references/change-impact-matrix.md), select every affected row, and put its downstream consumers and required evidence into the plan. A plan that names only the edited module is incomplete.

## Plan and implement

- State the user-visible symptom, confirmed evidence, root cause status (`confirmed` or `pending verification`), target architecture, in-scope files, excluded boundaries, and completion conditions.
- Compare a local patch with a systemic repair when ownership, representation, lifecycle, or timing crosses modules. Choose from evidence, not diff size.
- Add or change contracts and failing tests before implementation. Cover the first broken boundary and the final user-visible boundary; avoid asserting implementation wording alone.
- For scheduling, freezing, current/history, or Web visibility changes, use the hot/cold five-period matrix in the authoritative design. A single timestamp or fixture is insufficient.
- For state or JSON changes, trace the typed value from owner to final serializer and browser consumer. Do not add dictionary fallbacks or parallel status sources.
- For concurrency or deadlines, account separately for queue wait, vendor attempts, validation, persistence, publication, cancellation, and shutdown.

## Diagnose and verify

Read [the runtime diagnostics guide](references/runtime-diagnostics.md) whenever the task touches running behavior, suppliers, history, Web, performance, or browser delivery. Use `scripts/diagnose_runtime.py` as the only public diagnostic CLI. Keep each implementation in its owning `scripts/runtime_diagnostics/` module; do not recreate standalone wrapper scripts or duplicate probe logic in the orchestrator.

Read [the delivery evidence guide](references/delivery-evidence.md) before marking implementation or Review complete. It defines the minimum root-cause, regression, live-process, diff, and handoff evidence.

Do not claim live verification from mocks, HTTP 200 alone, an old process, or source code inspection. If real service, supplier, token, browser, or time-window evidence is unavailable, record the precise unverified gate and keep the claim bounded.
