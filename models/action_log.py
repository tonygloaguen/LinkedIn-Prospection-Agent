"""Pydantic model for action log entries."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ActionType = Literal["search", "scrape", "score", "message", "connect", "error"]


class ActionLog(BaseModel):
    """A single logged action in the pipeline.

    Attributes:
        timestamp: ISO timestamp when the action occurred.
        action_type: Category of action performed.
        profile_id: Optional profile involved in the action.
        post_id: Optional post involved in the action.
        payload: Arbitrary JSON-serializable payload for context.
        success: Whether the action succeeded.
        error_message: Error description if success is False.
    """

    timestamp: str
    action_type: ActionType
    profile_id: str | None = None
    post_id: str | None = None
    payload: dict[str, Any] | None = Field(default=None)
    success: bool = True
    error_message: str | None = None
