"""LangGraph StateGraph assembly for the LinkedIn Prospection Agent."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import structlog
from langgraph.graph import END, StateGraph

from agent.exceptions import LinkedInAuthError, QuotaExceededException
from agent.state import LinkedInProspectionState
from utils.metrics import create_run_metrics

logger = structlog.get_logger(__name__)


def build_graph() -> StateGraph:
    """Build and compile the LinkedIn Prospection StateGraph.

    The pipeline runs sequentially (MAX_CONCURRENT=1) to respect RPi
    memory constraints and LinkedIn rate limits.

    Returns:
        Compiled LangGraph StateGraph.
    """
    graph = StateGraph(LinkedInProspectionState)

    # Node wrappers are defined in run_pipeline() to inject dependencies.
    # The graph is compiled with node names only; actual callables are
    # injected at runtime via run_pipeline().

    graph.add_node("search_posts", _noop)
    graph.add_node("extract_profiles", _noop)
    graph.add_node("enrich_profile", _noop)
    graph.add_node("score_profile", _noop)
    graph.add_node("generate_message", _noop)
    graph.add_node("send_connection", _noop)
    graph.add_node("follow_up_scheduler", _noop)
    graph.add_node("log_action", _noop)

    graph.set_entry_point("search_posts")
    graph.add_edge("search_posts", "extract_profiles")
    graph.add_edge("extract_profiles", "enrich_profile")
    graph.add_edge("enrich_profile", "score_profile")
    graph.add_edge("score_profile", "generate_message")
    graph.add_edge("generate_message", "send_connection")
    graph.add_edge("send_connection", "follow_up_scheduler")
    graph.add_edge("follow_up_scheduler", "log_action")
    graph.add_edge("log_action", END)

    return graph


async def _noop(state: LinkedInProspectionState) -> LinkedInProspectionState:
    """No-op placeholder node (replaced at runtime by run_pipeline).

    Args:
        state: Current state.

    Returns:
        State unchanged.
    """
    return state


async def run_pipeline(
    keywords: list[str],
    max_invitations: int = 15,
    max_actions: int = 40,
    dry_run: bool = False,
) -> LinkedInProspectionState:
    """Execute the full LinkedIn Prospection pipeline.

    Initialises browser, database, and state; wires up all nodes;
    runs the LangGraph StateGraph to completion.

    Args:
        keywords: List of search keywords.
        max_invitations: Max invitations to send per run.
        max_actions: Max total actions per run.
        dry_run: If True, skip actual invitation sending.

    Returns:
        Final pipeline state after all nodes have executed.

    Raises:
        LinkedInAuthError: If LinkedIn authentication fails.
        QuotaExceededException: If daily quota is reached before pipeline starts.
    """
    import aiosqlite

    from playwright_linkedin.auth import login
    from playwright_linkedin.browser import BrowserManager
    from storage.database import init_db

    from agent.nodes.enrich_profile import enrich_profile
    from agent.nodes.extract_profiles import extract_profiles
    from agent.nodes.follow_up_scheduler import follow_up_scheduler
    from agent.nodes.generate_message import generate_message
    from agent.nodes.log_action import log_action
    from agent.nodes.score_profile import score_profile
    from agent.nodes.search_posts import search_posts
    from agent.nodes.send_connection import send_connection

    db_path = os.environ.get("DB_PATH", "./data/linkedin.db")

    await init_db(db_path)

    initial_state: LinkedInProspectionState = {
        "keywords": keywords,
        "max_invitations": max_invitations,
        "max_actions": max_actions,
        "dry_run": dry_run,
        "collected_posts": [],
        "candidate_profiles": [],
        "scored_profiles": [],
        "messages_generated": {},
        "invitations_sent": [],
        "actions_count": 0,
        "errors": [],
        "run_metrics": create_run_metrics(),
    }

    async with BrowserManager() as (_, context):
        page = await login(context)

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")

            state = initial_state

            node_sequence = [
                ("search_posts", lambda s: search_posts(s, page, db)),
                ("extract_profiles", lambda s: extract_profiles(s, page, db)),
                ("enrich_profile", lambda s: enrich_profile(s, page, db)),
                ("score_profile", lambda s: score_profile(s, db)),
                ("generate_message", lambda s: generate_message(s, db)),
                ("send_connection", lambda s: send_connection(s, page, db)),
                ("follow_up_scheduler", lambda s: follow_up_scheduler(s, db)),
                ("log_action", lambda s: log_action(s, db)),
            ]

            for node_name, node_fn in node_sequence:
                logger.info("node_start", node=node_name)
                try:
                    state = await node_fn(state)
                    logger.info("node_done", node=node_name)
                except (LinkedInAuthError, QuotaExceededException) as exc:
                    logger.warning(
                        "pipeline_stopped",
                        node=node_name,
                        reason=type(exc).__name__,
                        error=str(exc),
                    )
                    # Still run log_action to persist metrics
                    try:
                        from agent.nodes.log_action import log_action as _log
                        state = await _log(state, db)
                    except Exception:
                        pass
                    break
                except Exception as exc:
                    logger.error(
                        "node_unexpected_error",
                        node=node_name,
                        error=str(exc),
                    )
                    errors = list(state["errors"])
                    errors.append(f"{node_name}: {exc}")
                    state = {**state, "errors": errors}
                    # Continue to next node

    return state
