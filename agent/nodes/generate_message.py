"""generate_message node: LLM-based connection message generation."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import structlog

from agent.exceptions import LLMUnavailableError, MessageGenerationError, QuotaExceededException
from agent.state import LinkedInProspectionState
from models.profile import ScoredProfile
from utils.llm_client import call_llm

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parents[2] / "prompts"
_MESSAGE_MAX_CHARS = 280
_MESSAGE_MIN_CHARS = 100
_MIN_SCORE_TO_MESSAGE = 0.3


def _load_prompt(filename: str) -> str:
    """Load a prompt template file.

    Args:
        filename: Filename relative to the prompts/ directory.

    Returns:
        Raw prompt template string.
    """
    return (_PROMPTS_DIR / filename).read_text()


def _select_prompt_template(profile: ScoredProfile) -> str:
    """Select the appropriate prompt template based on profile category.

    Args:
        profile: Scored profile to generate message for.

    Returns:
        Formatted prompt string ready for LLM submission.
    """
    if profile.profile_category == "recruiter":
        template = _load_prompt("generate_connection_recruiter.txt")
        notable_detail = profile.headline or profile.bio or "professionnel LinkedIn"
        return template.format(
            full_name=profile.full_name or "Inconnu",
            headline=profile.headline or "N/A",
            notable_detail=notable_detail[:100],
        )
    else:
        # technical | cto_ciso | other
        template = _load_prompt("generate_connection_technical.txt")
        common_interest = _detect_common_interest(profile)
        return template.format(
            full_name=profile.full_name or "Inconnu",
            headline=profile.headline or "N/A",
            common_interest=common_interest,
        )


def _detect_common_interest(profile: ScoredProfile) -> str:
    """Detect a common technical interest from profile data.

    Args:
        profile: ScoredProfile to analyze.

    Returns:
        Short string describing the detected common interest.
    """
    text = f"{profile.headline or ''} {profile.bio or ''}".lower()

    interest_keywords = [
        ("LangGraph", ["langgraph", "langchain"]),
        ("LLMOps / agents IA", ["llmops", "llm", "gpt", "agent ia", "ai agent"]),
        ("DevSecOps", ["devsecops", "devops", "sécurité"]),
        ("Kubernetes / Docker", ["kubernetes", "k8s", "docker"]),
        ("Prometheus / Grafana", ["prometheus", "grafana", "observabilité"]),
        ("cybersécurité", ["cybersécurité", "soc", "blue team", "pentest", "siem"]),
        ("SRE / Platform Engineering", ["sre", "platform engineering", "reliability"]),
        ("Python", ["python", "fastapi", "asyncio"]),
        ("infrastructure open source", ["open source", "on-prem", "self-hosted"]),
    ]

    for label, keys in interest_keywords:
        if any(k in text for k in keys):
            return label

    return "infra et DevSecOps"


def _sanitize_message(raw: str) -> str:
    """Clean and trim LLM output to a valid LinkedIn message.

    Args:
        raw: Raw LLM response text.

    Returns:
        Cleaned message string, max 280 characters.
    """
    msg = raw.strip().strip('"').strip("'")
    # Remove common LLM prefixes
    for prefix in ["Voici le message :", "Message :", "Réponse :"]:
        if msg.startswith(prefix):
            msg = msg[len(prefix) :].strip()
    return msg[:_MESSAGE_MAX_CHARS]


async def generate_message(
    state: LinkedInProspectionState,
    db: object,
) -> LinkedInProspectionState:
    """Generate a personalised connection message for each scored profile.

    Only generates messages for profiles with score_total >= _MIN_SCORE_TO_MESSAGE.
    Uses recruiter prompt for recruiter category, technical prompt for others.

    Args:
        state: Current pipeline state with scored_profiles.
        db: Active aiosqlite database connection.

    Returns:
        Updated state with messages_generated populated.
    """
    from datetime import datetime

    from models.action_log import ActionLog
    from storage.queries import log_action

    messages: dict[str, str] = dict(state["messages_generated"])
    errors = list(state["errors"])
    actions_count = state["actions_count"]

    # Sort by score descending, limit to top profiles for messaging
    candidates = sorted(
        state["scored_profiles"],
        key=lambda p: p.score_total,
        reverse=True,
    )

    remaining_invites = state["max_invitations"] - len(state["invitations_sent"])
    candidates = candidates[:remaining_invites]

    for profile in candidates:
        if actions_count >= state["max_actions"]:
            raise QuotaExceededException(f"Max actions ({state['max_actions']}) reached")

        if profile.id in messages:
            continue

        if profile.score_total < _MIN_SCORE_TO_MESSAGE:
            logger.debug(
                "profile_below_threshold", name=profile.full_name, score=profile.score_total
            )
            continue

        try:
            prompt = _select_prompt_template(profile)
            raw_message = await call_llm(prompt)
            message = _sanitize_message(raw_message)

            if len(message) < _MESSAGE_MIN_CHARS:
                raise MessageGenerationError(
                    f"Generated message too short ({len(message)} chars): {message!r}"
                )

            messages[profile.id] = message
            actions_count += 1

            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="message",
                    profile_id=profile.id,
                    payload={"length": len(message), "category": profile.profile_category},
                    success=True,
                ),
            )

            logger.info(
                "message_generated",
                name=profile.full_name,
                category=profile.profile_category,
                message_length=len(message),
            )

        except LLMUnavailableError as exc:
            errors.append(f"message:llm:{profile.id}: {exc}")
            logger.error("message_llm_failed", profile_id=profile.id, error=str(exc))

        except MessageGenerationError as exc:
            errors.append(f"message:gen:{profile.id}: {exc}")
            logger.warning("message_gen_failed", profile_id=profile.id, error=str(exc))

        except Exception as exc:
            errors.append(f"message:{profile.id}: {exc}")
            logger.error("message_unexpected", profile_id=profile.id, error=str(exc))

    return {
        **state,
        "messages_generated": messages,
        "actions_count": actions_count,
        "errors": errors,
    }
