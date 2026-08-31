# Research Application Boundary

- Own production-isolated research audits, bounded background evidence consumers, offline research services, and research-specific ports.
- Keep package initialization empty of aggregate imports so production loads only explicitly wired background consumers.
- Depend only on application ports/services and pure domain values; never import infrastructure, Web, or entrypoints.
- Preserve immutable identities, hashes, idempotency, `production_authority=false`, and disabled automatic profile switching.
