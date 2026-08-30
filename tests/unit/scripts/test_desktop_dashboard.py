from __future__ import annotations

from pathlib import Path
from runpy import run_path

_RUNNER = Path(__file__).resolve().parents[2] / "performance" / "run_desktop_dashboard.py"
_cleanup_browser_profile = run_path(str(_RUNNER))["_cleanup_browser_profile"]


class _FlakyProfile:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def cleanup(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("profile still in use")


def test_chrome_profile_cleanup_retries_a_bounded_filesystem_race() -> None:
    profile = _FlakyProfile(failures=2)

    _cleanup_browser_profile(profile, attempts=3, delay_seconds=0.0)

    assert profile.calls == 3
