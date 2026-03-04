"""score_profile node: LLM scoring for each candidate profile."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from agent.exceptions import LLMUnavailableError, QuotaExceededException
from agent.state import LinkedInProspectionState
from models.action_log import ActionLog
from models.profile import Profile, ProfileCategory, ScoredProfile
from utils.llm_client import call_llm

logger = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parents[2] / "prompts" / "score_profile.txt"
_SCORE_WEIGHTS = {"recruiter": 0.4, "technical": 0.4, "activity": 0.2}
_MIN_SCORE_THRESHOLD = 0.3


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


async def score_profile(
    state: LinkedInProspectionState,
    db: object,
) -> LinkedInProspectionState:
    """Score all candidate profiles using Gemini LLM.

    Calls the LLM for each profile with headline + bio + location.
    Saves scored profiles to the database.
    On LLM failure after retries, assigns score=0 and category=other.

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

    for profile in state["candidate_profiles"]:
        if actions_count >= state["max_actions"]:
            raise QuotaExceededException(f"Max actions ({state['max_actions']}) reached")

        if profile.id in already_scored_ids:
            continue

        try:
            prompt = _build_scoring_prompt(profile)
            response = await call_llm(prompt)
            scored_p = _parse_score_response(response, profile)
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
                    },
                    success=True,
                ),
            )

            logger.info(
                "profile_scored",
                name=profile.full_name,
                score=scored_p.score_total,
                category=scored_p.profile_category,
            )

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            errors.append(f"score:parse:{profile.id}: {exc}")
            logger.warning("score_parse_error", profile_id=profile.id, error=str(exc))
            # Assign zero scores
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
