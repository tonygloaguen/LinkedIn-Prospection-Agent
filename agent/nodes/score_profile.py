"""score_profile node: LLM scoring for each candidate profile."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from agent.exceptions import GeminiDailyQuotaError, LLMUnavailableError, QuotaExceededException
from agent.state import LinkedInProspectionState
from models.action_log import ActionLog
from models.profile import Profile, ProfileCategory, ScoredProfile
from utils.llm_client import call_llm

logger = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parents[2] / "prompts" / "score_profile.txt"
_SCORE_WEIGHTS = {"recruiter": 0.4, "technical": 0.4, "activity": 0.2}
_MIN_SCORE_THRESHOLD = 0.3

# Keywords used by the heuristic scorer when LLM daily quota is exhausted
_RECRUITER_TERMS = (
    "recruiter", "recruteur", "talent", "acquisition", "hiring", "head hunt", "rh ",
)
_TECH_TERMS = (
    "devops", "devsecops", "cloud", "engineer", "ingénieur", "sre", "platform",
    "security", "sécurité", "k8s", "kubernetes", "docker", "terraform", "python",
    "observab", "mlops", "llmops", "cto", "ciso", "infrastructure", "iac",
)


def _load_prompt_template() -> str:
    """Load the scoring prompt template from disk.

    Returns:
        Raw prompt template string.
    """
    return _PROMPT_PATH.read_text()


def _build_scoring_prompt(profile: Profile) -> str:
    """Build the full LLM prompt for profile scoring.

    Args:
        profile: Profile to score.

    Returns:
        Formatted prompt string.
    """
    template = _load_prompt_template()
    return template.format(
        full_name=profile.full_name or "Inconnu",
        headline=profile.headline or "N/A",
        bio=profile.bio or "N/A",
        location=profile.location or "N/A",
    )


def _parse_score_response(response: str, profile: Profile) -> ScoredProfile:
    """Parse the LLM JSON scoring response into a ScoredProfile.

    Args:
        response: Raw LLM text response (expected to be valid JSON).
        profile: Original profile to enrich with scores.

    Returns:
        ScoredProfile with populated scoring fields.
    """
    # Strip markdown code fences if present
    clean = response.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        clean = "\n".join(lines[1:-1]) if len(lines) > 2 else clean

    data: dict[str, Any] = json.loads(clean)

    score_recruiter = float(data.get("score_recruiter", 0.0))
    score_technical = float(data.get("score_technical", 0.0))
    score_activity = float(data.get("score_activity", 0.0))
    category: ProfileCategory = data.get("category", "other")
    reasoning = str(data.get("reasoning", ""))

    score_total = (
        _SCORE_WEIGHTS["recruiter"] * score_recruiter
        + _SCORE_WEIGHTS["technical"] * score_technical
        + _SCORE_WEIGHTS["activity"] * score_activity
    )

    profile_dict = profile.model_dump()
    profile_dict.update(
        {
            "score_recruiter": score_recruiter,
            "score_technical": score_technical,
            "score_activity": score_activity,
            "score_total": round(score_total, 4),
            "profile_category": category,
            "is_recruiter": (category == "recruiter"),
            "is_technical": (category in ("technical", "cto_ciso")),
            "reasoning": reasoning,
        }
    )
    return ScoredProfile(**profile_dict)


def _heuristic_score(profile: Profile) -> ScoredProfile:
    """Compute heuristic scores without LLM when the daily quota is exhausted.

    Uses keyword matching on headline and bio to estimate recruiter/technical
    probability. Profiles with headline + bio typically score above the 0.3
    send-connection threshold.

    Args:
        profile: Profile to score heuristically.

    Returns:
        ScoredProfile with estimated scores and reasoning='heuristic_daily_quota_fallback'.
    """
    headline = (profile.headline or "").lower()
    bio = (profile.bio or "").lower()
    combined = f"{headline} {bio}"

    is_recruiter = any(t in headline for t in _RECRUITER_TERMS)
    has_tech = any(t in combined for t in _TECH_TERMS)
    has_bio = profile.bio is not None and len(profile.bio) > 30

    score_recruiter = 0.65 if is_recruiter else 0.1
    score_technical = min((0.55 if has_tech else 0.15) + (0.15 if has_bio else 0.0), 0.8)
    score_activity = 0.55 if has_bio else 0.3

    score_total = round(
        _SCORE_WEIGHTS["recruiter"] * score_recruiter
        + _SCORE_WEIGHTS["technical"] * score_technical
        + _SCORE_WEIGHTS["activity"] * score_activity,
        4,
    )

    if is_recruiter:
        category: ProfileCategory = "recruiter"
    elif has_tech:
        category = "technical"
    else:
        category = "other"

    profile_dict = profile.model_dump()
    profile_dict.update(
        {
            "score_recruiter": round(score_recruiter, 4),
            "score_technical": round(score_technical, 4),
            "score_activity": round(score_activity, 4),
            "score_total": score_total,
            "profile_category": category,
            "is_recruiter": is_recruiter,
            "is_technical": (category in ("technical", "cto_ciso")),
            "reasoning": "heuristic_daily_quota_fallback",
        }
    )
    return ScoredProfile(**profile_dict)


async def score_profile(
    state: LinkedInProspectionState,
    db: object,
) -> LinkedInProspectionState:
    """Score all candidate profiles using Gemini LLM.

    Calls the LLM for each profile with headline + bio + location.
    Saves scored profiles to the database.

    When the Gemini daily quota is exhausted (GeminiDailyQuotaError), switches
    to heuristic scoring for all remaining profiles so the pipeline can continue
    to send_connection without blocking on LLM failures.

    A configurable inter-call delay (GEMINI_INTER_CALL_DELAY_S, default 2s)
    is applied between LLM calls to reduce minute-level rate-limit pressure.

    Args:
        state: Current pipeline state with candidate_profiles.
        db: Active aiosqlite database connection.

    Returns:
        Updated state with scored_profiles populated.
    """
    from storage.queries import log_action, upsert_scored_profile

    scored: list[ScoredProfile] = list(state["scored_profiles"])
    already_scored_ids: set[str] = {p.id for p in scored}
    errors = list(state["errors"])
    actions_count = state["actions_count"]

    inter_call_delay = float(os.environ.get("GEMINI_INTER_CALL_DELAY_S", "2"))
    daily_quota_exhausted = False

    for profile in state["candidate_profiles"]:
        if actions_count >= state["max_actions"]:
            raise QuotaExceededException(f"Max actions ({state['max_actions']}) reached")

        if profile.id in already_scored_ids:
            continue

        try:
            if daily_quota_exhausted:
                # Daily quota already known exhausted — use heuristic directly, no LLM call
                scored_p = _heuristic_score(profile)
                logger.info(
                    "profile_scored_heuristic",
                    name=profile.full_name,
                    score=scored_p.score_total,
                    category=scored_p.profile_category,
                )
            else:
                prompt = _build_scoring_prompt(profile)
                response = await call_llm(prompt)
                scored_p = _parse_score_response(response, profile)
                # Throttle between LLM calls to reduce per-minute rate limit pressure
                await asyncio.sleep(inter_call_delay)
                logger.info(
                    "profile_scored",
                    name=profile.full_name,
                    score=scored_p.score_total,
                    category=scored_p.profile_category,
                )

            scored.append(scored_p)
            already_scored_ids.add(scored_p.id)
            actions_count += 1

            await upsert_scored_profile(db, scored_p)  # type: ignore[arg-type]
            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="score",
                    profile_id=profile.id,
                    payload={
                        "score_total": scored_p.score_total,
                        "category": scored_p.profile_category,
                        "method": "heuristic" if daily_quota_exhausted else "llm",
                    },
                    success=True,
                ),
            )

        except GeminiDailyQuotaError as exc:
            # Daily quota exhausted — switch to heuristic for this and all remaining profiles
            daily_quota_exhausted = True
            errors.append(f"score:llm:{profile.id}: {exc}")
            logger.warning(
                "gemini_daily_quota_fallback_mode",
                profile_id=profile.id,
                quota_detail=str(exc),
                hint="Remaining profiles will be scored heuristically",
            )
            scored_p = _heuristic_score(profile)
            scored.append(scored_p)
            already_scored_ids.add(scored_p.id)
            actions_count += 1

            await upsert_scored_profile(db, scored_p)  # type: ignore[arg-type]
            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="score",
                    profile_id=profile.id,
                    payload={
                        "score_total": scored_p.score_total,
                        "category": scored_p.profile_category,
                        "method": "heuristic_daily_quota_fallback",
                    },
                    success=True,
                ),
            )

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            errors.append(f"score:parse:{profile.id}: {exc}")
            logger.warning("score_parse_error", profile_id=profile.id, error=str(exc))
            fallback = ScoredProfile(**profile.model_dump(), reasoning="parse_error")
            scored.append(fallback)
            already_scored_ids.add(fallback.id)

        except LLMUnavailableError as exc:
            errors.append(f"score:llm:{profile.id}: {exc}")
            logger.error("llm_unavailable", profile_id=profile.id, error=str(exc))
            fallback = ScoredProfile(**profile.model_dump(), reasoning="llm_unavailable")
            scored.append(fallback)
            already_scored_ids.add(fallback.id)
            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="error",
                    profile_id=profile.id,
                    payload={"reason": "llm_unavailable"},
                    success=False,
                    error_message=str(exc),
                ),
            )

        except Exception as exc:
            errors.append(f"score:{profile.id}: {exc}")
            logger.error("score_unexpected_error", profile_id=profile.id, error=str(exc))
            fallback = ScoredProfile(**profile.model_dump(), reasoning="unexpected_error")
            scored.append(fallback)
            already_scored_ids.add(fallback.id)

    metrics = dict(state["run_metrics"])
    metrics["profiles_scored"] = len(scored)

    return {
        **state,
        "scored_profiles": scored,
        "actions_count": actions_count,
        "errors": errors,
        "run_metrics": metrics,  # type: ignore[typeddict-item]
    }
