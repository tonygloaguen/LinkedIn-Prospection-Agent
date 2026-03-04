"""RunMetrics aggregation utilities."""

from __future__ import annotations

from datetime import UTC, datetime

from agent.state import RunMetrics


def create_run_metrics(start_time: str | None = None) -> RunMetrics:
    """Create a fresh RunMetrics with default zero values.

    Args:
        start_time: ISO timestamp; defaults to now if not provided.

    Returns:
        Initialized RunMetrics TypedDict.
    """
    return RunMetrics(
        posts_found=0,
        profiles_extracted=0,
        profiles_scored=0,
        invitations_sent=0,
        errors_count=0,
        start_time=start_time or datetime.now(UTC).isoformat(),
        end_time=None,
    )


def finalize_metrics(metrics: RunMetrics) -> RunMetrics:
    """Set the end_time on a RunMetrics instance.

    Args:
        metrics: Existing RunMetrics to finalize.

    Returns:
        Updated RunMetrics with end_time set to now.
    """
    return RunMetrics(**{**metrics, "end_time": datetime.now(UTC).isoformat()})
