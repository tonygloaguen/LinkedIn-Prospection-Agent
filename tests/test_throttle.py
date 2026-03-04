"""Tests for throttle and rate limiting logic."""

from __future__ import annotations

import pytest

from agent.exceptions import QuotaExceededException
from utils.throttle import check_activity_window


class TestActivityWindow:
    """Tests for the activity window check."""

    def test_check_activity_window_raises_outside_hours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raises QuotaExceededException outside 08h-20h."""
        import utils.throttle as throttle_module

        monkeypatch.setattr(throttle_module, "_current_hour", lambda: 3)

        with pytest.raises(QuotaExceededException, match="activity window"):
            check_activity_window()

    def test_check_activity_window_passes_during_hours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Does not raise during 08h-20h."""
        import utils.throttle as throttle_module

        monkeypatch.setattr(throttle_module, "_current_hour", lambda: 12)
        check_activity_window()  # Should not raise

    def test_boundary_hour_8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hour 8 is within the activity window."""
        import utils.throttle as throttle_module

        monkeypatch.setattr(throttle_module, "_current_hour", lambda: 8)
        check_activity_window()

    def test_boundary_hour_20_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hour 20 is outside the activity window (exclusive upper bound)."""
        import utils.throttle as throttle_module

        monkeypatch.setattr(throttle_module, "_current_hour", lambda: 20)
        with pytest.raises(QuotaExceededException):
            check_activity_window()
