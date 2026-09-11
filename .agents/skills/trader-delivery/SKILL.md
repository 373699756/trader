---
name: trader-delivery
description: Plan, implement, diagnose, review, and verify Trader A-share dashboard changes across market data, scoring, DeepSeek, scheduling, freezing, persistence, API/SSE, Web, research evidence, performance, and release boundaries. Use for repository mutations and engineering reviews; skip purely explanatory questions that require no repository analysis or change.
---

# Trader Delivery

Deliver one repository change without reopening a known failure or regressing an adjacent boundary.

## Start the batch

1. Follow the repository `AGENTS.md`; this skill adds routing and evidence requirements but does not duplicate or override it.
2. Record `HEAD`, `@{upstream}`, staged and unstaged files, and the exact task file scope before editing. Preserve unrelated user changes.
3. Search `CHANGELOG.md` and `docs/changelog/` for the symptom, error code, affected strategy, likely boundary, and `Regression-Key`. Open only matching records; use the legacy archive only when current records point there. Treat old root causes as leads, not current facts.
4. Read the applicable authoritative contract before planning: `docs/02_工程设计.md` for product, architecture, runtime, API, Web, operations, and acceptance; `docs/01_评分逻辑.md` for candidates, scoring, risk, DeepSeek, fusion, action, ranking, and profit-validation gates. Use `docs/03_工程实施.md` for current task order/status and `docs/04_策略回溯.md` when historical data, training, or model-to-production flow is involved; neither overrides the two authoritative contracts.
5. Read [the change-impact matrix](references/change-impact-matrix.md), select every affected row, and put its downstream consumers and required evidence into the plan. A plan that names only the edited module is incomplete.

## Plan and implement

- State the user-visible symptom, confirmed evidence, root cause status (`confirmed` or `pending verification`), target architecture, in-scope files, excluded boundaries, and completion conditions.
- Compare a local patch with a systemic repair when ownership, representation, resource orchestration, or timing crosses modules. Choose from evidence, not diff size.
- Add or change contracts and failing tests before implementation. Cover the first broken boundary and the final user-visible boundary; avoid asserting implementation wording alone.
- For scheduling, freezing, current/history, or Web visibility changes, use the hot/cold five-period matrix in the authoritative design. A single timestamp or fixture is insufficient.
- For state or JSON changes, trace the typed value from owner to final serializer and browser consumer. Do not add dictionary fallbacks or parallel status sources.
- For candidate, score, model diagnostic, cost gate, risk, fusion, action, ranking, decision identity, or score research changes, read [the scoring-chain guide](references/scoring-chain.md) and trace the changed meaning through every listed owner and consumer.
- For concurrency or deadlines, account separately for queue wait, vendor attempts, validation, persistence, publication, cancellation, and shutdown.

## Diagnose and verify

Read [the runtime diagnostics guide](references/runtime-diagnostics.md) whenever the task touches running behavior, suppliers, history, Web, performance, or browser delivery. Use `scripts/diagnose_runtime.py` as the only public diagnostic CLI. Keep each implementation in its owning `scripts/runtime_diagnostics/` module; do not recreate standalone wrapper scripts or duplicate probe logic in the orchestrator.

For empty recommendations, a stuck collecting state, unexpected funnel counts, or disagreement between Web and runtime status, read [the recommendation-funnel incident playbook](references/recommendation-funnel-incidents.md) before assigning a root cause. Complete all six checkpoints even when the first probe appears decisive. A sandbox `connection_failed`, a generic runtime error category, HTTP 200, or a fixture browser pass is not enough to close a funnel incident.

Read [the delivery evidence guide](references/delivery-evidence.md) before marking implementation or Review complete. It defines the minimum root-cause, regression, live-process, diff, and handoff evidence.

Do not claim live verification from mocks, HTTP 200 alone, an old process, or source code inspection. If real service, supplier, token, browser, or time-window evidence is unavailable, record the precise unverified gate and keep the claim bounded.

When this skill or its references change, verify that linked files, named source/test paths, commands, API routes, configuration paths, and stable identities still exist. A syntax-only skill validator cannot prove routing correctness.

This skill is a repository delivery workflow loaded by the agent under `AGENTS.md`; it is not a product runtime hook and will never be triggered by Web, market-data, or scheduler events. If it was not loaded for an eligible repository mutation, record and correct the process omission instead of adding product trigger logic.
