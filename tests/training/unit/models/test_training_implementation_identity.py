"""Sample and model reuse must observe the download-owned training projection."""

from pathlib import Path

from trader.training.infra.implementation_identity import TrainingImplementationIdentity


def test_changed_download_training_projection_invalidates_implementation_identity(monkeypatch):
    original = Path.read_bytes
    observed = []

    def changed(path):
        content = original(path)
        if path.as_posix().endswith("/download/infra/history_prices.py"):
            observed.append(path)
            return content + b"\n# fixture: changed training projection\n"
        return content

    TrainingImplementationIdentity.cache_clear()
    try:
        previous = TrainingImplementationIdentity()
        monkeypatch.setattr(Path, "read_bytes", changed)
        TrainingImplementationIdentity.cache_clear()
        current = TrainingImplementationIdentity()
        assert len(observed) == 1 and current != previous
    finally:
        TrainingImplementationIdentity.cache_clear()
