# Web API Boundary

- Own HTTP route validation, explicit JSON projection, SSE response encoding, and injected read-only Web services.
- Depend only on application decision queries/events, domain values, Flask, and presentation release constants.
- Do not import `infra`, entrypoints, the composition root, suppliers, scorers, DeepSeek clients, or persistence writers.
- Keep URL, schema, ETag, cursor, error, and release-handshake behavior covered by E8 and app-factory contracts.
