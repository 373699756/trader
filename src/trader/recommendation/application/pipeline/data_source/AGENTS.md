# Market-Data Application Boundary

- Own typed market refresh outcomes, scored-input assembly, draft construction, and application-level data-plane orchestration.
- Depend on application ports, recommendation/decision use cases, runtime scheduling contracts, and domain values. External suppliers, persistence, and clocks must remain injected.
- Do not import `infra`, Flask, Web adapters, or perform hidden network/database construction. Preserve local-first publication and explicit degraded input quality.
- Direct verification: `tests/unit/application/test_input_runtime.py`, `tests/integration/test_scheduler_runtime.py`, and the E3/E4/E5/E6/E7 contracts.
