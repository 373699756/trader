"""Bounded temporary text files, released only after publication receipts exist."""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from threading import Lock


class CompanyDocumentFiles:
    def __init__(
        self,
        root: Path,
        *,
        max_text_characters: int = 1_000_000,
        max_bytes: int = 256 * 1024 * 1024,
        reserve_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if min(max_text_characters, max_bytes, reserve_bytes) <= 0:
            raise ValueError("company_document_capacity_invalid")
        self.root = root
        self._max_text_bytes = max_text_characters * 4
        self._max_bytes = max_bytes
        self._reserve_bytes = reserve_bytes
        self._lock = Lock()

    def _path(self, digest: bytes) -> Path:
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise ValueError("company_document_digest_invalid")
        key = digest.hex()
        return self.root / key[:2] / (key + ".txt.gz")

    def batch_limit(self, requested: int) -> int:
        """Reserve worst-case text capacity for every result admitted to this batch."""
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            used = sum(path.stat().st_size for path in self.root.glob("*/*.txt.gz"))
            available = min(self._max_bytes - used, shutil.disk_usage(self.root).free - self._reserve_bytes)
            count = min(requested, max(0, available // (self._max_text_bytes + 4096)))
            if count < 1:
                raise RuntimeError("company_temporary_space_exhausted")
            return count

    def write(self, text: str) -> bytes:
        raw = text.encode("utf-8")
        if not raw or len(raw) > self._max_text_bytes:
            raise ValueError("company_document_text_size_invalid")
        digest = hashlib.sha256(raw).digest()
        target = self._path(digest)
        payload = gzip.compress(raw, compresslevel=1, mtime=0)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            used = sum(path.stat().st_size for path in self.root.glob("*/*.txt.gz"))
            if (
                used + len(payload) > self._max_bytes
                or shutil.disk_usage(self.root).free < len(payload) + self._reserve_bytes
            ):
                raise RuntimeError("company_temporary_space_exhausted")
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pending-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return digest

    def read(self, digest: bytes) -> str:
        path = self._path(digest)
        if path.stat().st_size > self._max_text_bytes + 4096:
            raise ValueError("company_document_compressed_size_invalid")
        with gzip.open(path, "rb") as stream:
            raw = stream.read(self._max_text_bytes + 1)
        if len(raw) > self._max_text_bytes or hashlib.sha256(raw).digest() != digest:
            raise ValueError("company_document_integrity_invalid")
        return raw.decode("utf-8")

    def release(self, retained: frozenset[bytes]) -> None:
        """Caller pins the collection writer while identifying and removing unreferenced files."""
        with self._lock:
            if not self.root.exists():
                return
            keep = {digest.hex() for digest in retained}
            for path in self.root.glob("*/*.txt.gz"):
                if path.name.removesuffix(".txt.gz") not in keep:
                    path.unlink(missing_ok=True)
            for path in self.root.glob("*/.pending-*"):
                path.unlink(missing_ok=True)


__all__ = ["CompanyDocumentFiles"]
