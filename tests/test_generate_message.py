"""Tests for the generate_message node logic."""

from __future__ import annotations

from agent.nodes.generate_message import (
    _detect_common_interest,
    _sanitize_message,
    _select_prompt_template,
)
from models.profile import ScoredProfile


class TestMessageGeneration:
    """Tests for message generation helpers."""

    def test_sanitize_removes_leading_quotes(self) -> None:
        """_sanitize_message strips surrounding quotes."""
        assert _sanitize_message('"Hello there"') == "Hello there"
        assert _sanitize_message("'Hello there'") == "Hello there"

    def test_sanitize_truncates_to_280(self) -> None:
        """_sanitize_message truncates at 280 characters."""
        long_msg = "x" * 400
        assert len(_sanitize_message(long_msg)) == 280

    def test_sanitize_removes_llm_prefix(self) -> None:
        """_sanitize_message strips common LLM output prefixes."""
        msg = "Voici le message : Bonjour"
        assert _sanitize_message(msg) == "Bonjour"

    def test_detect_common_interest_langgraph(self) -> None:
        """Detects LangGraph as common interest from headline."""
        profile = ScoredProfile(
            linkedin_url="https://x.com/test",
            headline="AI engineer building LangGraph pipelines",
        )
        interest = _detect_common_interest(profile)
        assert "LangGraph" in interest

    def test_detect_common_interest_default(self) -> None:
        """Returns default interest when no keywords match."""
        profile = ScoredProfile(
            linkedin_url="https://x.com/test",
            headline="Marketing Manager",
        )
        interest = _detect_common_interest(profile)
        assert interest  # Should return some default string

    def test_select_prompt_recruiter(self) -> None:
        """Recruiter profile uses recruiter prompt template."""
        profile = ScoredProfile(
            linkedin_url="https://x.com/test",
            full_name="Alice Recru",
            headline="Tech Recruiter",
            profile_category="recruiter",
        )
        prompt = _select_prompt_template(profile)
        assert "recruteur" in prompt.lower() or "recruiter" in prompt.lower() or "Génère" in prompt

    def test_select_prompt_technical(self) -> None:
        """Technical profile uses technical prompt template."""
        profile = ScoredProfile(
            linkedin_url="https://x.com/test",
            full_name="Bob Dev",
            headline="DevSecOps Engineer",
            profile_category="technical",
        )
        prompt = _select_prompt_template(profile)
        assert "pairs techniques" in prompt or "collégial" in prompt
