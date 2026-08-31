# Runtime Application Boundary

- Own scheduling, cadence, source and strategy lanes, latest-wins workers, lifecycle, shutdown deadlines, and typed runtime status.
- Depend only on application ports, application decision/research collaborators, domain values, and explicitly injected callables or resources.
- Do not import `infra`, Flask, Web adapters, or construct external clients. Keep queue wait, vendor attempts, validation, persistence, publication, cancellation, and shutdown timing observable as separate stages.
- Direct verification: `tests/unit/application/test_{cadence,schedule,workers,v2_lifecycle,runtime}.py`, `tests/integration/test_v2_scheduler_runtime.py`, and `tests/contract/test_v2_e3_runtime_contract.py`.
