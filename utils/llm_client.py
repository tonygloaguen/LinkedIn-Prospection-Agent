"""Gemini LLM client with async support and retry on rate limits."""

from __future__ import annotations

import asyncio
import os

import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from agent.exceptions import LLMUnavailableError

logger = structlog.get_logger(__name__)


class _GeminiRateLimitError(Exception):
    """Internal signal for Gemini rate limit errors."""


class _GeminiAPIError(Exception):
    """Internal signal for Gemini API errors."""


def _get_gemini_client() -> object:
    """Lazily import and configure the google-generativeai client.

    Returns:
        Configured GenerativeModel instance.

    Raises:
        LLMUnavailableError: If GEMINI_API_KEY is not set.
    """
    try:
        import google.generativeai as genai  # type: ignore[import]
    except ImportError as exc:
        raise LLMUnavailableError("google-generativeai package not installed") from exc

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise LLMUnavailableError("GEMINI_API_KEY environment variable is not set")

    model_name = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
    genai.configure(api_key=api_key)  # type: ignore[attr-defined]
    return genai.GenerativeModel(model_name)  # type: ignore[attr-defined]


async def _call_gemini_once(prompt: str) -> str:
    """Send a single prompt to Gemini and return the text response.

    Args:
        prompt: Full prompt string.

    Returns:
        Text response from Gemini.

    Raises:
        _GeminiRateLimitError: On HTTP 429 / resource exhausted.
        _GeminiAPIError: On any other Gemini error.
    """
    model = _get_gemini_client()

    try:
        # google-generativeai generate_content is synchronous; run in executor
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, model.generate_content, prompt)  # type: ignore[attr-defined]
        return str(response.text)
    except Exception as exc:
        exc_str = str(exc).lower()
        if "429" in exc_str or "resource_exhausted" in exc_str or "rate" in exc_str:
            logger.warning("gemini_rate_limit", error=str(exc))
            raise _GeminiRateLimitError(str(exc)) from exc
        logger.error("gemini_api_error", error=str(exc))
        raise _GeminiAPIError(str(exc)) from exc


@retry(
    retry=retry_if_exception_type(_GeminiAPIError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    reraise=True,
)
async def _call_gemini_with_retry(prompt: str) -> str:
    """Call Gemini with automatic retry on transient API errors.

    Args:
        prompt: Full prompt string.

    Returns:
        Text response from Gemini.
    """
    return await _call_gemini_once(prompt)


async def call_llm(prompt: str) -> str:
    """Call Gemini with retry on rate limit.

    Retries once after a 60-second wait if a rate limit is hit.
    Uses tenacity for general API error retries (3 attempts).

    Args:
        prompt: Full prompt to send to the model.

    Returns:
        Text response from the model.

    Raises:
        LLMUnavailableError: If Gemini fails after all retries.
    """
    try:
        return str(await _call_gemini_with_retry(prompt))
    except _GeminiRateLimitError:
        logger.warning("gemini_rate_limit_sleeping", seconds=60)
        await asyncio.sleep(60)
        try:
            return await _call_gemini_once(prompt)
        except (_GeminiRateLimitError, _GeminiAPIError) as exc:
            raise LLMUnavailableError(f"Gemini rate-limited after retry: {exc}") from exc
    except _GeminiAPIError as exc:
        raise LLMUnavailableError(f"Gemini failed after retries: {exc}") from exc
    except LLMUnavailableError:
        raise
