"""Tests for utils/rtk.py and utils/diagnose.py."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from utils.rtk import _python_fallback_gain, rtk_gain, rtk_gain_file


def _metrics(
    posts: int = 0,
    profiles: int = 0,
    invitations: int = 0,
    errors: int = 0,
) -> dict:
    """Build a minimal run_metrics dict for tests."""
    return {
        "posts_found": posts,
        "profiles_extracted": profiles,
        "invitations_sent": invitations,
        "errors_count": errors,
    }


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_JSON_LOGS = "\n".join(
    [
        json.dumps(
            {
                "event": "node_start",
                "logger": "agent.graph",
                "level": "info",
                "node": "search_posts",
            }
        ),
        json.dumps(
            {
                "event": "post_skipped_missing_author",
                "logger": "playwright_linkedin.search",
                "level": "warning",
                "keyword": "DevOps",
                "has_url": False,
                "has_snippet": False,
            }
        ),
        json.dumps(
            {
                "event": "debug_info_noise",
                "logger": "utils.anti_detection",
                "level": "debug",
                "detail": "mouse jitter applied",
            }
        ),
        json.dumps(
            {
                "event": "run_complete",
                "logger": "agent.nodes.log_action",
                "level": "info",
                "posts_found": 0,
                "invitations_sent": 0,
            }
        ),
        json.dumps(
            {
                "event": "some_low_signal_event",
                "logger": "utils.throttle",
                "level": "info",
                "detail": "rate limit sleep",
            }
        ),
    ]
)


# ── Python fallback tests ─────────────────────────────────────────────────────


class TestPythonFallbackGain:
    def test_keeps_warning_lines(self):
        result = _python_fallback_gain(SAMPLE_JSON_LOGS)
        assert "post_skipped_missing_author" in result

    def test_keeps_high_signal_events(self):
        result = _python_fallback_gain(SAMPLE_JSON_LOGS)
        assert "node_start" in result
        assert "run_complete" in result

    def test_drops_debug_lines(self):
        result = _python_fallback_gain(SAMPLE_JSON_LOGS)
        assert "debug_info_noise" not in result

    def test_drops_low_signal_info_lines(self):
        result = _python_fallback_gain(SAMPLE_JSON_LOGS)
        assert "some_low_signal_event" not in result

    def test_keeps_non_json_lines(self):
        mixed = "Starting LinkedIn Prospection Agent\n" + SAMPLE_JSON_LOGS
        result = _python_fallback_gain(mixed)
        assert "Starting LinkedIn Prospection Agent" in result

    def test_returns_original_if_nothing_kept(self):
        """When no line matches, return original to avoid silent data loss."""
        only_noise = json.dumps({"event": "boring_debug", "level": "debug", "detail": "x"})
        result = _python_fallback_gain(only_noise)
        assert result == only_noise

    def test_empty_string(self):
        assert _python_fallback_gain("") == ""

    def test_whitespace_only(self):
        result = _python_fallback_gain("   \n\n  ")
        # All lines empty — nothing kept, return original
        assert result.strip() == ""


# ── rtk_gain with mocked subprocess ──────────────────────────────────────────


class TestRtkGain:
    def test_uses_python_fallback_when_rtk_disabled(self, monkeypatch):
        monkeypatch.setenv("RTK_ENABLED", "false")
        result = rtk_gain(SAMPLE_JSON_LOGS)
        # Python fallback should have filtered the logs
        assert "run_complete" in result
        assert "boring_debug" not in result

    def test_uses_python_fallback_when_rtk_not_found(self, monkeypatch):
        monkeypatch.setenv("RTK_ENABLED", "true")
        monkeypatch.delenv("RTK_BIN", raising=False)
        with patch("utils.rtk.shutil.which", return_value=None):
            result = rtk_gain(SAMPLE_JSON_LOGS)
        assert "run_complete" in result

    def test_uses_rtk_when_available(self, monkeypatch, tmp_path):
        """RTK binary found → subprocess called → output returned."""
        fake_rtk = tmp_path / "rtk"
        fake_rtk.write_text("#!/bin/sh\ncat\n")
        fake_rtk.chmod(0o755)

        monkeypatch.setenv("RTK_ENABLED", "true")
        monkeypatch.setenv("RTK_BIN", str(fake_rtk))

        result = rtk_gain("hello world")
        assert result == "hello world"

    def test_falls_back_on_rtk_failure(self, monkeypatch, tmp_path):
        """RTK exits non-zero → Python fallback used."""
        fake_rtk = tmp_path / "rtk"
        fake_rtk.write_text("#!/bin/sh\nexit 1\n")
        fake_rtk.chmod(0o755)

        monkeypatch.setenv("RTK_ENABLED", "true")
        monkeypatch.setenv("RTK_BIN", str(fake_rtk))

        result = rtk_gain(SAMPLE_JSON_LOGS)
        # Should fall back to Python filter
        assert "run_complete" in result

    def test_empty_input_returns_empty(self, monkeypatch):
        monkeypatch.setenv("RTK_ENABLED", "false")
        assert rtk_gain("") == ""
        assert rtk_gain("   ") == ""


# ── rtk_gain_file ─────────────────────────────────────────────────────────────


class TestRtkGainFile:
    def test_reads_and_filters_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RTK_ENABLED", "false")  # use Python fallback
        log_file = tmp_path / "agent.log"
        log_file.write_text(SAMPLE_JSON_LOGS)
        result = rtk_gain_file(log_file)
        assert "run_complete" in result

    def test_returns_empty_for_missing_file(self):
        result = rtk_gain_file("/nonexistent/path/agent.log")
        assert result == ""


# ── generate_run_digest ───────────────────────────────────────────────────────


class TestGenerateRunDigest:
    def test_creates_digest_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("RTK_ENABLED", "false")

        from utils.diagnose import generate_run_digest

        log_file = tmp_path / "agent.log"
        log_file.write_text(SAMPLE_JSON_LOGS)

        path = generate_run_digest(
            run_id="test-run-1234-abcd",
            log_file=str(log_file),
            metrics=_metrics(),
            errors=[],
        )
        assert path is not None
        assert Path(path).exists()
        content = Path(path).read_text()
        assert "METRICS" in content
        assert "Posts found" in content

    def test_digest_includes_errors(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("RTK_ENABLED", "false")

        from utils.diagnose import generate_run_digest

        path = generate_run_digest(
            run_id="test-run-errors",
            log_file=None,
            metrics=_metrics(errors=2),
            errors=["Error A", "Error B"],
        )
        assert path is not None
        content = Path(path).read_text()
        assert "Error A" in content
        assert "Error B" in content

    def test_status_empty_when_no_posts(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("RTK_ENABLED", "false")

        from utils.diagnose import generate_run_digest

        path = generate_run_digest(
            run_id="test-empty",
            log_file=None,
            metrics=_metrics(),
            errors=[],
        )
        assert path is not None
        content = Path(path).read_text()
        assert "EMPTY" in content

    def test_status_success_when_invitations_sent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("RTK_ENABLED", "false")

        from utils.diagnose import generate_run_digest

        path = generate_run_digest(
            run_id="test-success",
            log_file=None,
            metrics=_metrics(posts=10, profiles=5, invitations=3),
            errors=[],
        )
        assert path is not None
        content = Path(path).read_text()
        assert "SUCCESS" in content

    def test_returns_none_on_unwritable_dir(self, monkeypatch, tmp_path):
        """Digest returns None when the log dir cannot be written to."""
        unwritable = tmp_path / "noperm"
        unwritable.mkdir()
        unwritable.chmod(0o000)

        monkeypatch.setenv("LOG_DIR", str(unwritable))

        if os.getuid() == 0:
            pytest.skip("Running as root — permission check not enforceable")

        from utils.diagnose import generate_run_digest

        path = generate_run_digest(
            run_id="test-fail",
            log_file=None,
            metrics={},
            errors=[],
        )
        assert path is None

        # Restore permissions so tmp_path cleanup works
        unwritable.chmod(0o755)

    def test_rtk_section_included_on_error(self, monkeypatch, tmp_path):
        """RTK section appended when errors > 0."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("RTK_ENABLED", "false")

        log_file = tmp_path / "agent.log"
        log_file.write_text(SAMPLE_JSON_LOGS)

        from utils.diagnose import generate_run_digest

        path = generate_run_digest(
            run_id="test-rtk-section",
            log_file=str(log_file),
            metrics=_metrics(errors=1),
            errors=["some error"],
        )
        assert path is not None
        content = Path(path).read_text()
        assert "LOG SUMMARY" in content

    def test_rtk_section_omitted_on_clean_run(self, monkeypatch, tmp_path):
        """RTK section NOT appended when run is clean and posts found."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("RTK_ENABLED", "false")

        log_file = tmp_path / "agent.log"
        log_file.write_text(SAMPLE_JSON_LOGS)

        from utils.diagnose import generate_run_digest

        path = generate_run_digest(
            run_id="test-clean",
            log_file=str(log_file),
            metrics=_metrics(posts=5, profiles=3, invitations=2),
            errors=[],
        )
        assert path is not None
        content = Path(path).read_text()
        assert "LOG SUMMARY" not in content
