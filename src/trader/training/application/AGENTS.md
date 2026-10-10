# Training Evaluation Application Boundary

- Own production-isolated research audits, bounded background evidence consumers, offline evaluation services, outcome settlement, and their narrow ports.
- Keep package initialization empty of aggregate imports so production loads only explicitly wired background consumers.
- Consume immutable frozen decisions, market observations, and research evidence; never change scores, actions, ranking, freeze records, or production configuration.
- Depend only on application ports/services and pure domain values; never import business infrastructure, Web, entrypoints, suppliers, or DeepSeek clients. Shared technical imports are limited to worker protocols (`WorkerExecutor`, `ManagedWorkerExecutor`), the stateless `submit_or_reject` helper, and immutable shutdown values (`ShutdownDeadline`, `ShutdownStep`). Concrete executors remain owned and injected by `bootstrap.py`.
- Preserve immutable identities, hashes, idempotency, `production_authority=false`, and disabled automatic profile switching.
