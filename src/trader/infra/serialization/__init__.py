"""Neutral infrastructure capabilities for persisted artifacts.

The package holds the serialization, sealing and decoding primitives that every
artifact repository in this layer needs. It carries no business semantics, so
the scoring and research packages can share artifact identity without importing
one another.
"""
