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

from agent.exceptions import GeminiDailyQuotaError, LLMUnavailableError

logger = structlog.get_logger(__name__)

# Markers in Gemini error messages that identify a daily (non-recoverable) quota exhaustion.
# These differ from per-minute rate limits which resolve after a short wait.
_DAILY_QUOTA_MARKERS = (
    "generate_content_free_tier_requests",  # Free-tier RPD quota id
    "generaterequestsperdayperproject",  # Quota id (lowercase match)
    "requests_per_day",
    "requests per day",
    "daily limit",
)


class _GeminiRateLimitError(Exception):
    """Internal signal: Gemini per-minute rate limit (recoverable after a short wait)."""


class _GeminiDailyQuotaError(Exception):
    """Internal signal: Gemini daily quota exhausted (non-recoverable this run)."""


class _GeminiAPIError(Exception):
    """Internal signal: Gemini API error (transient, retriable)."""


def _is_daily_quota_error(error_str: str) -> bool:
    """Return True if the error string indicates a daily (non-recoverable) quota exhaustion."""
    lower = error_str.lower()
    return any(marker in lower for marker in _DAILY_QUOTA_MARKERS)


def _get_gemini_client(model_override: str | None = None) -> tuple[object, str]:
    """Lazily import and configure the google-genai client.

    Args:
        model_override: If provided, use this model name instead of LLM_MODEL env var.

    Returns:
        Tuple of (Client instance, model name string).

    Raises:
        LLMUnavailableError: If google-genai is not installed or GEMINI_API_KEY is not set.
    """
    try:
        from google import genai  # type: ignore[import]
    except ImportError as exc:
        raise LLMUnavailableError("google-genai package not installed") from exc

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise LLMUnavailableError("GEMINI_API_KEY environment variable is not set")

    model_name = model_override or os.environ.get("LLM_MODEL", "gemini-2.0-flash")
    client = genai.Client(api_key=api_key)  # type: ignore[attr-defined]
    return client, model_name


async def _call_gemini_once(prompt: str, model_override: str | None = None) -> str:
    """Send a single prompt to Gemini and return the text response.

    Args:
        prompt: Full prompt string.
        model_override: If provided, use this model instead of the default.

    Returns:
        Text response from Gemini.

    Raises:
        _GeminiDailyQuotaError: On daily free-tier quota exhaustion (non-recoverable).
        _GeminiRateLimitError: On per-minute HTTP 429 / resource exhausted (recoverable).
        _GeminiAPIError: On any other Gemini error (transient, retriable).
    """
    client, model_name = _get_gemini_client(model_override)

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(  # type: ignore[attr-defined]
                model=model_name,
                contents=prompt,
            ),
        )
        return str(response.text)
    except Exception as exc:
        exc_str = str(exc)
        if "429" in exc_str or "resource_exhausted" in exc_str.lower() or "rate" in exc_str.lower():
            if _is_daily_quota_error(exc_str):
                logger.warning(
                    "gemini_daily_quota_exhausted",
                    model=model_name,
                    error=exc_str,
                )
                raise _GeminiDailyQuotaError(exc_str) from exc
            logger.warning("gemini_rate_limit", model=model_name, error=exc_str)
            raise _GeminiRateLimitError(exc_str) from exc
        logger.error("gemini_api_error", model=model_name, error=exc_str)
        raise _GeminiAPIError(exc_str) from exc


@retry(
    retry=retry_if_exception_type(_GeminiAPIError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    reraise=True,
)
async def _call_gemini_with_retry(prompt: str) -> str:
    """Call Gemini with automatic retry on transient API errors.

    Note: _GeminiDailyQuotaError and _GeminiRateLimitError are NOT retried here;
    they are handled in call_llm() with dedicated logic.

    Args:
        prompt: Full prompt string.

    Returns:
        Text response from Gemini.
    """
    return await _call_gemini_once(prompt)


async def call_llm(prompt: str) -> str:
    """Call Gemini with retry logic and daily quota fallback.

    Strategy:
    1. Try primary model with tenacity retry on transient errors.
    2. On per-minute rate limit: sleep 60s then retry once.
    3. On daily quota exhaustion: try GEMINI_FALLBACK_MODEL (default: gemini-1.5-flash).
    4. If fallback also daily-quota-exhausted: raise GeminiDailyQuotaError.

    Args:
        prompt: Full prompt to send to the model.

    Returns:
        Text response from the model.

    Raises:
        GeminiDailyQuotaError: If daily quota is exhausted on primary + fallback models.
        LLMUnavailableError: If Gemini fails after all retries for other reasons.
    """
    try:
        return str(await _call_gemini_with_retry(prompt))

    except _GeminiDailyQuotaError as primary_exc:
        # Daily quota exhausted on primary model — try fallback model
        primary_model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        fallback_model = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-1.5-flash")

        if fallback_model and fallback_model != primary_model:
            logger.warning(
                "gemini_daily_quota_trying_fallback",
                primary_model=primary_model,
                fallback_model=fallback_model,
            )
            try:
                return await _call_gemini_once(prompt, model_override=fallback_model)
            except _GeminiDailyQuotaError as fallback_exc:
                raise GeminiDailyQuotaError(
                    f"Daily quota exhausted on primary ({primary_model}) "
                    f"and fallback ({fallback_model}): {fallback_exc}"
                ) from fallback_exc
            except (_GeminiRateLimitError, _GeminiAPIError) as fallback_exc:
                raise GeminiDailyQuotaError(
                    f"Primary daily quota exhausted; fallback ({fallback_model}) "
                    f"also failed: {fallback_exc}"
                ) from fallback_exc

        raise GeminiDailyQuotaError(
            f"Daily quota exhausted on {primary_model}, no fallback configured "
            f"(set GEMINI_FALLBACK_MODEL): {primary_exc}"
        ) from primary_exc

    except _GeminiRateLimitError:
        # Per-minute rate limit — wait and retry once
        logger.warning("gemini_rate_limit_sleeping", seconds=60)
        await asyncio.sleep(60)
        try:
            return await _call_gemini_once(prompt)
        except _GeminiDailyQuotaError as exc:
            # Quota exhausted even after the wait — treat as daily quota
            raise GeminiDailyQuotaError(
                f"Daily quota exhausted after rate-limit sleep: {exc}"
            ) from exc
        except (_GeminiRateLimitError, _GeminiAPIError) as exc:
            raise LLMUnavailableError(f"Gemini rate-limited after retry: {exc}") from exc

    except _GeminiAPIError as exc:
        raise LLMUnavailableError(f"Gemini failed after retries: {exc}") from exc

    except LLMUnavailableError:
        raise
