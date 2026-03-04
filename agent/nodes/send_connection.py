"""send_connection node: send LinkedIn connection invitations."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import structlog

from agent.exceptions import (
    ConnectionSendError,
    LinkedInAuthError,
    QuotaExceededException,
)
from agent.state import LinkedInProspectionState
from models.action_log import ActionLog
from playwright_linkedin.connection import send_connection_invitation
from utils.throttle import check_activity_window, check_quotas, delay_after_invitation

logger = structlog.get_logger(__name__)


async def send_connection(
    state: LinkedInProspectionState,
    page: object,
    db: object,
) -> LinkedInProspectionState:
    """Send connection invitations to scored profiles with generated messages.

    Checks quotas before each invitation. Updates profile status in DB.
    Respects dry_run mode (logs but does not send).

    Args:
        state: Current pipeline state with scored_profiles and messages_generated.
        page: Authenticated Playwright Page.
        db: Active aiosqlite database connection.

    Returns:
        Updated state with invitations_sent populated.
    """
    from storage.queries import log_action, update_profile_status

    check_activity_window()

    db_path = os.environ.get("DB_PATH", "./data/linkedin.db")
    max_invitations = state["max_invitations"]
    max_actions = state["max_actions"]

    invitations_sent = list(state["invitations_sent"])
    errors = list(state["errors"])
    actions_count = state["actions_count"]
    dry_run = state["dry_run"]

    # Sort profiles by score descending
    sorted_profiles = sorted(
        state["scored_profiles"],
        key=lambda p: p.score_total,
        reverse=True,
    )

    for profile in sorted_profiles:
        if profile.id in invitations_sent:
            continue

        message = state["messages_generated"].get(profile.id)
        if not message:
            logger.debug("no_message_for_profile", profile_id=profile.id)
            continue

        if actions_count >= max_actions:
            raise QuotaExceededException(f"Max actions ({max_actions}) reached")

        if len(invitations_sent) >= max_invitations:
            raise QuotaExceededException(
                f"Max invitations per run ({max_invitations}) reached"
            )

        # Check DB quotas
        try:
            await check_quotas(db_path, max_invitations, max_actions, actions_count)
        except QuotaExceededException:
            raise

        try:
            sent = await send_connection_invitation(
                page,  # type: ignore[arg-type]
                profile.linkedin_url,
                message,
                dry_run=dry_run,
            )

            actions_count += 1

            if sent:
                invitations_sent.append(profile.id)

                await update_profile_status(
                    db,  # type: ignore[arg-type]
                    profile.id,
                    "messaged",
                )

                await log_action(
                    db,  # type: ignore[arg-type]
                    ActionLog(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        action_type="connect",
                        profile_id=profile.id,
                        payload={
                            "dry_run": dry_run,
                            "score": profile.score_total,
                            "category": profile.profile_category,
                        },
                        success=True,
                    ),
                )

                logger.info(
                    "invitation_sent",
                    name=profile.full_name,
                    dry_run=dry_run,
                    score=profile.score_total,
                )

                await delay_after_invitation()

            else:
                # Already connected — mark as connected
                await update_profile_status(db, profile.id, "connected")  # type: ignore[arg-type]
                logger.info("profile_already_connected", name=profile.full_name)

        except (QuotaExceededException, LinkedInAuthError):
            raise

        except ConnectionSendError as exc:
            errors.append(f"connect:{profile.id}: {exc}")
            logger.error(
                "connection_send_failed",
                profile_id=profile.id,
                error=str(exc),
            )
            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    action_type="error",
                    profile_id=profile.id,
                    success=False,
                    error_message=str(exc),
                ),
            )

        except Exception as exc:
            errors.append(f"connect:{profile.id}: {exc}")
            logger.error("connect_unexpected", profile_id=profile.id, error=str(exc))

    metrics = dict(state["run_metrics"])
    metrics["invitations_sent"] = len(invitations_sent)

    return {
        **state,
        "invitations_sent": invitations_sent,
        "actions_count": actions_count,
        "errors": errors,
        "run_metrics": metrics,  # type: ignore[typeddict-item]
    }
