"""Tests for the score_profile node logic."""

from __future__ import annotations

import json

from agent.nodes.score_profile import _build_scoring_prompt, _parse_score_response
from models.profile import Profile


class TestScoreProfileParsing:
    """Tests for LLM response parsing in score_profile node."""

    def test_parse_valid_json_response(self) -> None:
        """Parses a valid Gemini JSON response correctly."""
        profile = Profile(
            linkedin_url="https://www.linkedin.com/in/alice",
            full_name="Alice Smith",
            headline="DevSecOps Engineer",
        )
        response = json.dumps(
            {
                "score_recruiter": 0.1,
                "score_technical": 0.9,
                "score_activity": 0.7,
                "category": "technical",
                "reasoning": "Strong DevSecOps profile",
            }
        )

        scored = _parse_score_response(response, profile)

        assert scored.score_recruiter == 0.1
        assert scored.score_technical == 0.9
        assert scored.score_activity == 0.7
        assert scored.profile_category == "technical"
        assert scored.reasoning == "Strong DevSecOps profile"

    def test_score_total_weighted_formula(self) -> None:
        """score_total = 0.4 * recruiter + 0.4 * technical + 0.2 * activity."""
        profile = Profile(linkedin_url="https://x.com/test")
        response = json.dumps(
            {
                "score_recruiter": 1.0,
                "score_technical": 0.5,
                "score_activity": 0.0,
                "category": "recruiter",
                "reasoning": "",
            }
        )
        scored = _parse_score_response(response, profile)
        expected = 0.4 * 1.0 + 0.4 * 0.5 + 0.2 * 0.0
        assert abs(scored.score_total - expected) < 1e-6

    def test_parse_with_markdown_fences(self) -> None:
        """Handles LLM output wrapped in markdown code fences."""
        profile = Profile(linkedin_url="https://x.com/fences")
        raw = (
            "```json\n"
            '{"score_recruiter": 0.8, "score_technical": 0.2, '
            '"score_activity": 0.5, "category": "recruiter", "reasoning": "test"}\n'
            "```"
        )
        scored = _parse_score_response(raw, profile)
        assert scored.score_recruiter == 0.8

    def test_build_prompt_includes_profile_data(self) -> None:
        """Scoring prompt includes profile headline and bio."""
        profile = Profile(
            linkedin_url="https://x.com/p",
            full_name="Bob",
            headline="CTO at Startup",
            bio="Building AI agents",
            location="Paris",
        )
        prompt = _build_scoring_prompt(profile)
        assert "CTO at Startup" in prompt
        assert "Building AI agents" in prompt
        assert "Paris" in prompt
        assert "Bob" in prompt
