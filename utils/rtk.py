"""RTK (Rust Text Kit) wrapper for log summarization.

RTK is an optional external binary that summarizes noisy log streams via
information-gain filtering.  This module provides a subprocess wrapper with:

  - Env-var configuration (RTK_BIN, RTK_ENABLED, RTK_TIMEOUT)
  - Graceful fallback to a pure-Python JSON-log filter when RTK is absent
  - No hard dependency: the application never fails because RTK is missing

Environment variables
---------------------
RTK_ENABLED   true/false (default: true)  — disable entirely if needed
RTK_BIN       path to rtk binary          — overrides PATH lookup
RTK_TIMEOUT   seconds (default: 30)       — subprocess timeout

Public API
----------
    from utils.rtk import rtk_gain, rtk_gain_file

    summary = rtk_gain(log_text)        # summarize a string
    summary = rtk_gain_file(path)       # summarize a file
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# High-signal event names used by the Python fallback filter.
# Kept in sync with the event strings emitted by the agent nodes.
_HIGH_SIGNAL_EVENTS: frozenset[str] = frozenset(
    {
        "run_complete",
        "run_errors",
        "run_digest_saved",
        "node_start",
        "node_done",
        "enrich_node_summary",
        "enrich_warmup_pause",
        "cards_found_but_no_posts_extracted",
        "no_post_container_found",
        "debug_snapshot_saved",
        "debug_card_html_saved",
        "post_skipped_missing_author",
        "session_expired_detected",
        "linkedin_login_success",
        "linkedin_login_failed",
        "linkedin_consent_click_failed",
        "circuit_breaker_triggered",
        "quota_exceeded",
        "llm_call_failed",
        "llm_quota_exhausted",
        "profile_scored",
        "profile_scored_heuristic",
        "invitation_sent",
        "run_history_save_failed",
        "keyword_search_done",
        "profiles_extracted",
        "cookies_saved",
    }
)

_HIGH_SIGNAL_LEVELS: frozenset[str] = frozenset({"warning", "error", "critical"})


# ── Configuration ──────────────────────────────────────────────────────────────


def _resolve_rtk_bin() -> str | None:
    """Return the RTK binary path, or None if not available.

    Resolution order:
      1. RTK_BIN env var (absolute path, must be executable)
      2. ``rtk`` found anywhere in PATH
    """
    explicit = os.environ.get("RTK_BIN", "").strip()
    if explicit:
        p = Path(explicit)
        if p.is_file() and os.access(p, os.X_OK):
            return explicit
        logger.warning(
            "rtk_bin_not_executable",
            path=explicit,
            hint="Check RTK_BIN env var — binary not found or not executable",
        )

    found = shutil.which("rtk")
    return found or None


def _is_enabled() -> bool:
    return os.environ.get("RTK_ENABLED", "true").lower() not in ("false", "0", "no")


def _get_timeout() -> int:
    try:
        return int(os.environ.get("RTK_TIMEOUT", "30"))
    except ValueError:
        return 30


# ── Public API ─────────────────────────────────────────────────────────────────


def rtk_gain(text: str) -> str:
    """Summarize log text using ``rtk gain``.

    Tries the RTK binary first; falls back to a Python-based JSON log filter
    if RTK is unavailable or fails.  Never raises — always returns a string.

    Args:
        text: Raw log text (JSON lines from structlog, or plain text).

    Returns:
        Summarized / filtered text suitable for LLM consumption or human review.
        Returns the original text if no filtering could be applied.
    """
    if not text.strip():
        return ""

    if _is_enabled():
        rtk_bin = _resolve_rtk_bin()
        if rtk_bin:
            result = _run_rtk_gain(rtk_bin, text)
            if result is not None:
                return result
            # RTK ran but failed — fall through to Python fallback
            logger.info(
                "rtk_fallback_after_failure",
                hint="RTK binary failed; using Python JSON-log filter",
            )
        else:
            logger.debug(
                "rtk_not_found",
                hint="Set RTK_BIN env var or add rtk to PATH; using Python fallback",
            )

    return _python_fallback_gain(text)


def rtk_gain_file(path: str | Path) -> str:
    """Summarize a log file using ``rtk gain``.

    Args:
        path: Path to the log file.

    Returns:
        Summarized text, or empty string if file not found or unreadable.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("rtk_gain_file_not_found", path=str(p))
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        return rtk_gain(text)
    except Exception as exc:
        logger.warning("rtk_gain_file_error", path=str(p), error=str(exc))
        return ""


# ── Internal helpers ───────────────────────────────────────────────────────────


def _run_rtk_gain(rtk_bin: str, text: str) -> str | None:
    """Run ``rtk gain`` via subprocess.

    Args:
        rtk_bin: Absolute path to the rtk binary.
        text: Input text passed to stdin.

    Returns:
        stdout output on success, None on any failure.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [rtk_bin, "gain"],
            input=text,
            capture_output=True,
            text=True,
            timeout=_get_timeout(),
        )
        if proc.returncode == 0:
            logger.debug(
                "rtk_gain_ok",
                input_lines=text.count("\n"),
                output_lines=proc.stdout.count("\n"),
            )
            return proc.stdout
        logger.warning(
            "rtk_gain_nonzero_exit",
            returncode=proc.returncode,
            stderr=proc.stderr[:300] if proc.stderr else "",
        )
        return None
    except subprocess.TimeoutExpired:
        logger.warning("rtk_gain_timeout", timeout_s=_get_timeout())
        return None
    except Exception as exc:
        logger.warning("rtk_gain_subprocess_error", error=str(exc))
        return None


def _python_fallback_gain(text: str) -> str:
    """Pure-Python fallback: extract high-signal lines from a JSON log stream.

    Keeps:
      - All warning / error / critical level entries
      - Lines whose ``event`` field matches a curated high-signal set
      - Non-JSON lines that are longer than 10 chars (startup text, etc.)

    Args:
        text: Raw log text (JSON lines or mixed).

    Returns:
        Filtered text.  Returns the original text unchanged if nothing
        was filtered out (avoids silent data loss).
    """
    kept: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            level = (entry.get("level") or "").lower()
            event = entry.get("event") or ""
            if level in _HIGH_SIGNAL_LEVELS or event in _HIGH_SIGNAL_EVENTS:
                kept.append(line)
        except json.JSONDecodeError:
            # Non-JSON line (Docker header, startup echo, etc.) — keep if non-trivial
            if len(line) > 10:
                kept.append(line)

    if not kept:
        return text  # nothing filtered — return all rather than empty

    return "\n".join(kept)
