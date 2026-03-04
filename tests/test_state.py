"""Tests for the LangGraph state definition."""

from __future__ import annotations

from agent.state import LinkedInProspectionState
from utils.metrics import create_run_metrics, finalize_metrics


class TestRunMetrics:
    """Tests for RunMetrics creation and finalization."""

    def test_create_run_metrics_defaults(self) -> None:
        """create_run_metrics returns zero-valued metrics."""
        m = create_run_metrics()
        assert m["posts_found"] == 0
        assert m["profiles_extracted"] == 0
        assert m["profiles_scored"] == 0
        assert m["invitations_sent"] == 0
        assert m["errors_count"] == 0
        assert m["end_time"] is None
        assert m["start_time"] is not None

    def test_finalize_metrics_sets_end_time(self) -> None:
        """finalize_metrics sets end_time to a non-None value."""
        m = create_run_metrics()
        assert m["end_time"] is None
        finalized = finalize_metrics(m)
        assert finalized["end_time"] is not None

    def test_finalize_does_not_mutate_original(self) -> None:
        """finalize_metrics returns a new dict, does not mutate input."""
        m = create_run_metrics()
        finalized = finalize_metrics(m)
        assert m["end_time"] is None  # Original unchanged
        assert finalized["end_time"] is not None


class TestLinkedInProspectionState:
    """Tests for state structure validity."""

    def test_state_construction(self) -> None:
        """Can construct a valid LinkedInProspectionState."""
        state: LinkedInProspectionState = {
            "keywords": ["LangGraph agent"],
            "max_invitations": 15,
            "max_actions": 40,
            "dry_run": True,
            "collected_posts": [],
            "candidate_profiles": [],
            "scored_profiles": [],
            "messages_generated": {},
            "invitations_sent": [],
            "actions_count": 0,
            "errors": [],
            "run_metrics": create_run_metrics(),
        }

        assert state["dry_run"] is True
        assert state["max_invitations"] == 15
        assert len(state["keywords"]) == 1
