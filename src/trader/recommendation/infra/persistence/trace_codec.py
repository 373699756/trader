"""Recommendation-owned trace codec for immutable freeze observations."""

from __future__ import annotations

import hashlib
import zlib

COMPRESSED_PAYLOAD_PREFIX = b"trader-zlib\x00"
DEFAULT_DECODED_PAYLOAD_BYTES = 64 * 1024 * 1024


def PersistedPayloadBytes(payload: bytes) -> bytes:
    compressed = COMPRESSED_PAYLOAD_PREFIX + zlib.compress(payload, level=6)
    return compressed if len(compressed) < len(payload) else payload


def DecodedPayloadBytes(payload: bytes, *, maximum_bytes: int) -> bytes:
    if maximum_bytes < 1:
        raise ValueError("research trace decoded payload limit must be positive")
    if not payload.startswith(COMPRESSED_PAYLOAD_PREFIX):
        if len(payload) > maximum_bytes:
            raise ValueError("research trace decoded payload capacity exhausted")
        return payload
    compressed = payload[len(COMPRESSED_PAYLOAD_PREFIX) :]
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(compressed, maximum_bytes + 1)
    except zlib.error as exc:
        raise ValueError("research trace compressed payload is invalid") from exc
    if len(decoded) > maximum_bytes or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise ValueError("research trace decoded payload capacity exhausted")
    return decoded


def Sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "COMPRESSED_PAYLOAD_PREFIX",
    "DEFAULT_DECODED_PAYLOAD_BYTES",
    "DecodedPayloadBytes",
    "PersistedPayloadBytes",
    "Sha256",
]
