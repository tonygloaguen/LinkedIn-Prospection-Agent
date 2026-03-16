"""Post-run diagnostic digest generator.

Produces a compact, structured report of a completed pipeline run.
The digest is suitable for:

  - Human review in /logs/
  - LLM consumption in orchestration pipelines
  - Automated alerting / monitoring

The digest is written to ``/logs/digest_<run_id_short>.txt`` (level 1 — always)
and optionally an RTK-filtered log summary is appended (level 2 — only when
there is something to report: errors present, or 0 posts extracted).

Level 1: plain metrics + error list
Level 2: RTK-filtered log lines (appended when RTK is enabled and log file exists)

Usage
-----
    from utils.diagnose import generate_run_digest

    path = generate_run_digest(
        run_id="4ab77732-...",
        log_file="/logs/agent.log",
        metrics=state["run_metrics"],
        errors=state["errors"],
    )
    # path is "/logs/digest_4ab77732.txt" or None on failure
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import structlog

from utils.rtk import rtk_gain_file

logger = structlog.get_logger(__name__)

# Maximum number of RTK summary lines written to the digest.
_MAX_SUMMARY_LINES = 150


def generate_run_digest(
    run_id: str,
    log_file: str | None,
    metrics: dict,
    errors: list[str],
) -> str | None:
    """Generate a post-run RTK digest and write it to the log directory.

    Level 1 (always written): run ID, timestamp, metrics, status, errors.
    Level 2 (written when RTK_ENABLED and a log file exists):
      appended RTK-filtered log summary — triggered only when the run had
      errors or extracted 0 posts (avoids noisy output on clean runs).

    Args:
        run_id: UUID of the current run (used in output filename).
        log_file: Path to the structlog file written during this run.
                  May be None if LOG_FILE env var was not set.
        metrics: run_metrics dict from pipeline final state.
        errors: Error strings accumulated during the run.

    Returns:
        Absolute path to the written digest file, or None if skipped/failed.
    """
    log_dir = os.environ.get("LOG_DIR", "/logs")
    digest_path = Path(log_dir) / f"digest_{run_id[:8]}.txt"

    try:
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = _build_header(run_id)
        lines += _build_metrics_section(metrics)
        lines += _build_status_section(metrics)
        lines += _build_errors_section(errors)

        # Level 2: append RTK summary only when the run needs attention
        needs_attention = bool(errors) or metrics.get("posts_found", 0) == 0
        if needs_attention and log_file:
            lines += _build_rtk_section(log_file)

        lines.append("=" * 60)
        content = "\n".join(lines) + "\n"
        digest_path.write_text(content, encoding="utf-8")

        logger.info(
            "run_digest_saved",
            path=str(digest_path),
            errors_count=len(errors),
            rtk_section_included=needs_attention,
        )
        return str(digest_path)

    except Exception as exc:
        logger.warning("run_digest_failed", error=str(exc))
        return None


# ── Section builders ──────────────────────────────────────────────────────────


def _build_header(run_id: str) -> list[str]:
    return [
        "=" * 60,
        "LinkedIn Agent — Run Digest",
        f"Run ID  : {run_id}",
        f"At      : {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "=" * 60,
        "",
    ]


def _build_metrics_section(metrics: dict) -> list[str]:
    duration = _compute_duration(
        str(metrics.get("start_time", "")),
        str(metrics.get("end_time", "")),
    )
    return [
        "METRICS",
        "-" * 30,
        f"  Posts found        : {metrics.get('posts_found', 0)}",
        f"  Profiles extracted : {metrics.get('profiles_extracted', 0)}",
        f"  Profiles scored    : {metrics.get('profiles_scored', 0)}",
        f"  Invitations sent   : {metrics.get('invitations_sent', 0)}",
        f"  Errors             : {metrics.get('errors_count', 0)}",
        f"  Duration           : {duration}s",
        "",
    ]


def _build_status_section(metrics: dict) -> list[str]:
    status = _assess_status(metrics)
    return [f"STATUS: {status}", ""]


def _build_errors_section(errors: list[str]) -> list[str]:
    if not errors:
        return []
    lines = ["ERRORS", "-" * 30]
    for i, err in enumerate(errors[:10], 1):
        lines.append(f"  [{i}] {err}")
    if len(errors) > 10:
        lines.append(f"  ... and {len(errors) - 10} more (see log file)")
    lines.append("")
    return lines


def _build_rtk_section(log_file: str) -> list[str]:
    """Build the RTK-filtered log summary section."""
    summary = rtk_gain_file(log_file)
    if not summary:
        return []

    summary_lines = summary.splitlines()
    truncated = len(summary_lines) > _MAX_SUMMARY_LINES

    lines = ["LOG SUMMARY (RTK / filtered)", "-" * 30]
    lines.extend(summary_lines[:_MAX_SUMMARY_LINES])
    if truncated:
        lines.append(
            f"  ... ({len(summary_lines) - _MAX_SUMMARY_LINES} lines truncated"
            f" — see full log file)"
        )
    lines.append("")
    return lines


# ── Helpers ───────────────────────────────────────────────────────────────────


def _assess_status(metrics: dict) -> str:
    """Return a human-readable status string based on run metrics."""
    errors = metrics.get("errors_count", 0)
    posts = metrics.get("posts_found", 0)
    profiles = metrics.get("profiles_extracted", 0)
    invitations = metrics.get("invitations_sent", 0)

    if errors > 5:
        return f"DEGRADED — {errors} errors, manual review recommended"
    if posts == 0 and profiles == 0:
        return "EMPTY — no posts or profiles found (DOM/session issue likely)"
    if invitations > 0:
        return f"SUCCESS — {invitations} invitation(s) sent"
    if profiles > 0:
        return f"PARTIAL — {profiles} profile(s) found, 0 invitations sent"
    return "OK"


def _compute_duration(start: str, end: str) -> float:
    from datetime import datetime

    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        return round((e - s).total_seconds(), 1)
    except Exception:
        return 0.0
