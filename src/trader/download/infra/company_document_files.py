"""Bounded temporary text files, released only after publication receipts exist."""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from threading import Lock

from trader.infra.regular_file import OpenRegularFile


class CompanyDocumentFiles:
    def __init__(
        self,
        root: Path,
        *,
        max_text_characters: int,
        max_bytes: int,
        reserve_bytes: int,
    ) -> None:
        if min(max_text_characters, max_bytes, reserve_bytes) <= 0:
            raise ValueError("company_document_capacity_invalid")
        self.root = root
        self._max_text_bytes = max_text_characters * 4
        self._max_bytes = max_bytes
        self._reserve_bytes = reserve_bytes
        self._lock = Lock()
        self._pins: dict[bytes, int] = {}

    def _path(self, digest: bytes) -> Path:
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise ValueError("company_document_digest_invalid")
        key = digest.hex()
        return self.root / key[:2] / (key + ".txt.gz")

    def write(self, text: str) -> bytes:
        with self.prepare(text) as digest:
            return digest

    @contextmanager
    def prepare(
        self,
        text: str,
        *,
        reserve: Callable[[int], AbstractContextManager[object]] | None = None,
        check: Callable[[], None] | None = None,
    ) -> Iterator[bytes]:
        """Validate/reuse text and pin it until the caller commits its reference."""
        raw = text.encode("utf-8")
        if not raw or len(raw) > self._max_text_bytes:
            raise ValueError("company_document_text_size_invalid")
        digest = hashlib.sha256(raw).digest()
        target = self._path(digest)
        with self._lock:
            try:
                self.read(digest, check=check)
            except FileNotFoundError:
                payload = gzip.compress(raw, compresslevel=1, mtime=0)
                target.parent.mkdir(parents=True, exist_ok=True)
                used = sum(self._file_size(path) for path in self.root.glob("*/*.txt.gz"))
                if (
                    used + len(payload) > self._max_bytes
                    or shutil.disk_usage(self.root).free < len(payload) + self._reserve_bytes
                ):
                    raise RuntimeError("company_temporary_space_exhausted")
                reservation = reserve(len(payload)) if reserve is not None else nullcontext()
                with reservation:
                    self._write(target, payload)
            self._pins[digest] = self._pins.get(digest, 0) + 1
        try:
            yield digest
        finally:
            with self._lock:
                remaining = self._pins[digest] - 1
                if remaining:
                    self._pins[digest] = remaining
                else:
                    del self._pins[digest]

    def _write(self, target: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pending-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Publishing must never replace an unexpectedly appeared original.
            os.link(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, digest: bytes, *, check: Callable[[], None] | None = None) -> str:
        path = self._path(digest)
        chunks: list[bytes] = []
        size = 0
        with OpenRegularFile(path, maximum_bytes=self._max_text_bytes + 4096, check=check) as compressed:
            with gzip.GzipFile(fileobj=compressed, mode="rb") as stream:
                while chunk := stream.read(min(65536, self._max_text_bytes - size + 1)):
                    if check is not None:
                        check()
                    size += len(chunk)
                    if size > self._max_text_bytes:
                        raise ValueError("company_document_integrity_invalid")
                    chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw or hashlib.sha256(raw).digest() != digest:
            raise ValueError("company_document_integrity_invalid")
        return raw.decode("utf-8")

    @staticmethod
    def _file_size(path: Path) -> int:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("company_document_regular_file_required")
        return metadata.st_size

    def release(self, retained: frozenset[bytes]) -> None:
        """Caller pins the collection writer while identifying and removing unreferenced files."""
        with self._lock:
            if not self.root.exists():
                return
            keep = {digest.hex() for digest in retained | self._pins.keys()}
            for path in self.root.glob("*/*.txt.gz"):
                if path.name.removesuffix(".txt.gz") not in keep:
                    self._file_size(path)
                    path.unlink(missing_ok=True)
            for path in self.root.glob("*/.pending-*"):
                self._file_size(path)
                path.unlink(missing_ok=True)


__all__ = ["CompanyDocumentFiles"]
