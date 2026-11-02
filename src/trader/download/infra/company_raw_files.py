"""Durable company inputs and analysis artifacts with bounded disk reservations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock

from trader.download.domain.company_evidence import CompanyDocumentExtraction, CompanyDocumentOutputIdentity, CompanyOcrDiagnostic, Disclosure
from trader.infra.atomic_files import AtomicReadJson, AtomicWriteJson
from trader.infra.atomic_files.json import SyncDirectory


@dataclass(frozen=True)
class CompanyLocalDocument:
    directory: Path
    path: Path
    payload_hash: bytes
    size: int


@dataclass(frozen=True)
class _Reservation:
    key: str
    maximum: int
    initial: int
    analysis: bool


class CompanyRawFiles:
    """File owner; SQLite owns dispatch. Files are recoverable immutable outputs."""

    def __init__(self, root: Path, *, capacity: int, analysis_reserve: int, free_reserve: int) -> None:
        if not 0 < analysis_reserve < capacity or free_reserve <= 0:
            raise ValueError("company_raw_capacity_invalid")
        self.root = root
        self.capacity = capacity
        self.analysis_reserve = analysis_reserve
        self.free_reserve = free_reserve
        self._lock = RLock()
        self._reservations: list[_Reservation] = []

    def directory(self, item: Disclosure) -> Path:
        key = hashlib.sha256(json.dumps((item.source, item.identity, item.digest)).encode()).hexdigest()
        retained = self.root / "failed" / key[:2] / key
        return retained if retained.exists() else self.root / "documents" / key[:2] / key

    def retain_failure(
        self,
        item: Disclosure,
        failure: str,
        policy: str,
        attempted_at: str,
        diagnostic: CompanyOcrDiagnostic | None = None,
    ) -> Path:
        """Retain failed inputs and checkpoints permanently, including after recovery."""
        with self._lock:
            directory = self.directory(item)
            if any(r.key == str(directory) for r in self._reservations):
                raise RuntimeError("company_failed_input_in_use")
            target = self.root / "failed" / directory.parent.name / directory.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if directory != target and directory.exists():
                os.replace(directory, target)
            target.mkdir(exist_ok=True)
            identity = hashlib.sha256(f"{policy}:{attempted_at}".encode()).hexdigest()
            AtomicWriteJson(target / f"failure-{identity}.json", {
                "source": item.source, "source_key": item.identity, "record_hash": item.digest,
                "failure": failure, "policy": policy, "attempted_at": attempted_at,
                "diagnostic": asdict(diagnostic) if diagnostic is not None else None,
            })
            SyncDirectory(target.parent)
            return target

    def usage(self) -> int:
        return sum(self._size(self.root / name) for name in ("documents", "text", "failed"))

    @staticmethod
    def _size(directory: Path) -> int:
        def scan_error(error: OSError) -> None:
            if not isinstance(error, FileNotFoundError):
                raise error

        total = 0
        for root, _directories, files in os.walk(directory, onerror=scan_error):
            for name in files:
                path = Path(root) / name
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except FileNotFoundError:
                    continue  # Workers remove completed scratch files and directories.
        return total

    @contextmanager
    def reserve(self, directory: Path, maximum: int, *, analysis: bool = False) -> Iterator[None]:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            held = sum(max(0, r.maximum - max(0, self._size(Path(r.key)) - r.initial)) for r in self._reservations)
            ceiling = self.capacity if analysis else self.capacity - self.analysis_reserve
            if self.usage() + held + maximum > ceiling or shutil.disk_usage(self.root).free - held - maximum < self.free_reserve:
                raise RuntimeError("company_raw_capacity_exhausted")
            reservation = _Reservation(str(directory), maximum, self._size(directory), analysis)
            self._reservations.append(reservation)
            directory.mkdir(parents=True, exist_ok=True)
        try:
            yield
        finally:
            with self._lock:
                self._reservations.remove(reservation)

    def local(self, item: Disclosure) -> CompanyLocalDocument | None:
        directory = self.directory(item)
        files = tuple(directory.glob("*.pdf"))
        if not files:
            return None
        if len(files) != 1:
            raise ValueError("company_raw_file_identity_conflict")
        path = files[0]
        digest = self.digest(path)
        if path.stem != digest.hex():
            os.replace(path, path.with_suffix(".corrupt"))
            AtomicWriteJson(directory / "recovery.json", {"reason": "file_corrupt", "expected": path.stem, "actual": digest.hex()})
            return None
        return CompanyLocalDocument(directory, path, digest, path.stat().st_size)

    @staticmethod
    def digest(path: Path) -> bytes:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
        return digest.digest()

    def complete(self, item: Disclosure, partial: Path) -> CompanyLocalDocument:
        digest = self.digest(partial)
        with partial.open("rb") as stream:
            if b"%PDF-" not in stream.read(1024):
                raise ValueError("disclosure_document_unreadable")
            os.fsync(stream.fileno())
        target = partial.parent / (digest.hex() + ".pdf")
        with self._lock:
            os.replace(partial, target)
            # Content identity is independent of supplier metadata revisions.
            # Links keep the bytes alive until the last task releases its input.
            for other in (self.root / "documents").glob(f"*/*/{target.name}"):
                if other == target or self.digest(other) != digest:
                    continue
                shared = target.with_suffix(".shared")
                os.link(other, shared)
                os.replace(shared, target)
                result = other.parent / "result.json"
                checkpoints = tuple(other.parent.glob("ocr-*/*.json"))
                extra = sum(path.stat().st_size for path in checkpoints) + (result.stat().st_size if result.exists() else 0)
                with self.reserve(target.parent, extra + 4096, analysis=True):
                    if result.exists():
                        value = self._read_result(result)
                        if value is not None and value.get("payload") == digest.hex():
                            value["version"] = item.digest
                            value["checksum"] = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                            AtomicWriteJson(target.parent / "result.json", value)
                    for checkpoint in checkpoints:
                        destination = target.parent / checkpoint.parent.name / checkpoint.name
                        destination.parent.mkdir(exist_ok=True)
                        if not destination.exists():
                            os.link(checkpoint, destination)
                break
            SyncDirectory(target.parent)
        return CompanyLocalDocument(target.parent, target, digest, target.stat().st_size)

    @staticmethod
    def _read_result(path: Path) -> dict[str, object] | None:
        if not path.exists():
            return None
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("company_analysis_result_oversized")
        try:
            value = AtomicReadJson(path)
        except ValueError:
            os.replace(path, path.with_suffix(".corrupt"))
            return None
        if not isinstance(value, dict):
            return None
        checksum = value.pop("checksum", None)
        if checksum != hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest():
            os.replace(path, path.with_suffix(".corrupt"))
            return None
        return value

    def result(self, item: Disclosure, policies: tuple[str, ...]) -> CompanyDocumentExtraction | None:
        path = self.directory(item) / "result.json"
        value = self._read_result(path)
        if value is None or value.get("version") != item.digest:
            return None
        policy = value.get("policy")
        if not isinstance(policy, str) or CompanyDocumentOutputIdentity(policy) not in tuple(CompanyDocumentOutputIdentity(p) for p in policies):
            return None
        text, method, digest, policy, quality = (value.get(k) for k in ("text", "method", "payload", "policy", "quality"))
        if not isinstance(text, str) or method not in {"text", "ocr"} or not isinstance(digest, str) or not isinstance(policy, str):
            raise ValueError("company_analysis_result_invalid")
        if quality is not None and not isinstance(quality, (int, float)):
            raise ValueError("company_analysis_quality_invalid")
        return CompanyDocumentExtraction(text, method, bytes.fromhex(digest), policy, quality)

    def save_result(self, item: Disclosure, result: CompanyDocumentExtraction) -> None:
        payload = {
            "version": item.digest, "text": result.text, "method": result.method,
            "payload": result.payload_hash.hex(), "policy": result.policy_id, "quality": result.quality,
        }
        payload["checksum"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        AtomicWriteJson(self.directory(item) / "result.json", payload)

    def release(self, item: Disclosure) -> bool:
        directory = self.directory(item)
        with self._lock:
            if self.root / "failed" in directory.parents:
                return True
            if any(r.key == str(directory) for r in self._reservations):
                return False
            if directory.exists():
                shutil.rmtree(directory)
            return True


__all__ = ["CompanyLocalDocument", "CompanyRawFiles"]
