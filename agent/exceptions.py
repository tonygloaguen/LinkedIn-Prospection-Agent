"""Custom exception hierarchy for the LinkedIn Prospection Agent."""

from __future__ import annotations


class LinkedInAgentError(Exception):
    """Base exception for all agent errors."""


class LinkedInAuthError(LinkedInAgentError):
    """Raised when LinkedIn authentication fails.

    Pipeline behaviour: immediate stop, do not continue.
    """


class QuotaExceededException(LinkedInAgentError):  # noqa: N818
    """Raised when daily invitation or action quota is reached.

    Pipeline behaviour: clean stop, log metrics, exit 0.
    """


class ProfileScrapingError(LinkedInAgentError):
    """Raised when a profile cannot be scraped.

    Pipeline behaviour: skip profile, continue pipeline.
    """


class LLMUnavailableError(LinkedInAgentError):
    """Raised when the Gemini LLM is unavailable after retries.

    Pipeline behaviour: retry 3x via tenacity, then skip with score=0.
    """


class PlaywrightTimeoutError(LinkedInAgentError):
    """Raised when a Playwright action times out.

    Pipeline behaviour: take debug screenshot, continue.
    """


class PostSearchError(LinkedInAgentError):
    """Raised when a keyword search fails."""


class LinkedInSessionExpiredError(PostSearchError):
    """Raised when a search redirect indicates the session has expired.

    Pipeline behaviour: trigger mid-run re-authentication, then retry remaining keywords.
    If re-auth fails after 3 attempts, raise LinkedInAuthError to stop the pipeline cleanly.
    """


class MessageGenerationError(LinkedInAgentError):
    """Raised when LLM message generation fails."""


class ConnectionSendError(LinkedInAgentError):
    """Raised when the connection invitation send flow fails."""
