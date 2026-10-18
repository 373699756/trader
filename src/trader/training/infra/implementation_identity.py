"""Content identity for training and its pure feature/label dependencies."""

from functools import lru_cache
from hashlib import sha256
from pathlib import Path


@lru_cache(maxsize=1)
def TrainingImplementationIdentity() -> str:
    root = Path(__file__).resolve().parents[2]
    digest = sha256()
    for directory in ("training", "download/domain", "recommendation/domain"):
        for path in sorted((root / directory).rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(sha256(path.read_bytes()).digest())
    return digest.hexdigest()


__all__ = ["TrainingImplementationIdentity"]
