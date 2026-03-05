"""LinkedIn Prospection Agent — Typer CLI entrypoint."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Annotated

import structlog
import typer

app = typer.Typer(
    name="linkedin-agent",
    help="LinkedIn Prospection Agent — orchestrated by LangGraph",
    add_completion=False,
)

DEFAULT_KEYWORDS = [
    # IA / LLM
    "LangGraph agent",
    "LLM infrastructure",
    "AI agents Python",
    "RAG production",
    "LLMOps",
    "prompt engineering",
    # DevSecOps / Cyber
    "DevSecOps NIS2",
    "blue team SOC",
    "supply chain security",
    "infrastructure as code security",
    "SIEM Wazuh",
    # Infra / Observability
    "Prometheus Grafana on-prem",
    "platform engineering SRE",
    "open source infrastructure",
    "Docker Kubernetes sécurité",
]


def _setup_logging(log_level: str = "INFO", log_file: str | None = None) -> None:
    """Configure structlog for JSON-structured logging.

    Args:
        log_level: Logging level string (DEBUG, INFO, WARNING, ERROR).
        log_file: Optional path to write logs to file in addition to stdout.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,  # type: ignore[arg-type]
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        format="%(message)s",
        handlers=handlers,
        level=level,
    )


def _load_env() -> None:
    """Load .env file if present (dotenv optional, falls back gracefully)."""
    env_path = Path(".env")
    if env_path.exists():
        try:
            from dotenv import load_dotenv  # type: ignore[import]

            load_dotenv(env_path)
        except ImportError:
            # Manual parse for simple KEY=VALUE lines
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    if key.strip() not in os.environ:
                        os.environ[key.strip()] = value.strip()


@app.command()
def run(
    keywords: Annotated[
        list[str] | None,
        typer.Option("--keywords", "-k", help="Search keywords (can repeat)"),
    ] = None,
    max_invitations: Annotated[
        int,
        typer.Option("--max-invitations", help="Max invitations per run"),
    ] = 15,
    max_actions: Annotated[
        int,
        typer.Option("--max-actions", help="Max total actions per run"),
    ] = 40,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--no-dry-run", help="Dry run — no real invitations sent"),
    ] = False,
) -> None:
    """Run the full LinkedIn prospection pipeline.

    Searches LinkedIn for posts matching keywords, scrapes profiles,
    scores them with Gemini, generates personalised messages, and
    sends connection invitations.
    """
    _load_env()
    _setup_logging(
        os.environ.get("LOG_LEVEL", "INFO"),
        os.environ.get("LOG_FILE"),
    )

    kws = keywords or DEFAULT_KEYWORDS
    max_inv = int(os.environ.get("MAX_INVITATIONS_PER_DAY", str(max_invitations)))
    max_act = int(os.environ.get("MAX_ACTIONS_PER_DAY", str(max_actions)))
    is_dry = os.environ.get("DRY_RUN", "false").lower() == "true" or dry_run

    typer.echo("Starting LinkedIn Prospection Agent")
    typer.echo(f"  Keywords: {len(kws)}")
    typer.echo(f"  Max invitations: {max_inv}")
    typer.echo(f"  Max actions: {max_act}")
    typer.echo(f"  Dry run: {is_dry}")

    from agent.graph import run_pipeline

    final_state = asyncio.run(
        run_pipeline(
            keywords=kws,
            max_invitations=max_inv,
            max_actions=max_act,
            dry_run=is_dry,
        )
    )

    metrics = final_state["run_metrics"]
    typer.echo("\n=== Run Summary ===")
    typer.echo(f"Posts found:        {metrics.get('posts_found', 0)}")
    typer.echo(f"Profiles extracted: {metrics.get('profiles_extracted', 0)}")
    typer.echo(f"Profiles scored:    {metrics.get('profiles_scored', 0)}")
    typer.echo(f"Invitations sent:   {metrics.get('invitations_sent', 0)}")
    typer.echo(f"Errors:             {metrics.get('errors_count', 0)}")


@app.command(name="dry-run")
def dry_run_cmd(
    keywords: Annotated[
        list[str] | None,
        typer.Option("--keywords", "-k", help="Search keywords (can repeat)"),
    ] = None,
) -> None:
    """Run the pipeline in dry-run mode — no invitations sent.

    Useful for testing the search + scoring pipeline without
    actually sending connection requests.
    """
    _load_env()
    _setup_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FILE"))

    kws = keywords or DEFAULT_KEYWORDS[:3]

    typer.echo("Dry-run mode: no invitations will be sent")

    from agent.graph import run_pipeline

    asyncio.run(
        run_pipeline(
            keywords=kws,
            max_invitations=15,
            max_actions=40,
            dry_run=True,
        )
    )


@app.command()
def stats() -> None:
    """Display database statistics for the current run history."""
    _load_env()
    db_path = os.environ.get("DB_PATH", "./data/linkedin.db")

    if not Path(db_path).exists():
        typer.echo(f"No database found at {db_path}. Run the agent first.")
        raise typer.Exit(1)

    async def _show_stats() -> None:
        import aiosqlite

        from storage.queries import get_stats

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            s = await get_stats(db)

        typer.echo(f"\nTotal profiles:       {s['profiles_total']}")
        typer.echo(f"Invitations today:    {s['invitations_today']}")
        typer.echo(f"Actions today:        {s['actions_today']}")
        typer.echo(f"Invitations total:    {s['invitations_total']}")

        if s.get("by_category"):
            typer.echo("\nBy category:")
            for cat, count in s["by_category"].items():
                typer.echo(f"  {cat or 'unknown'}: {count}")

        if s.get("by_status"):
            typer.echo("\nBy status:")
            for status, count in s["by_status"].items():
                typer.echo(f"  {status or 'unknown'}: {count}")

        if s.get("top_profiles"):
            typer.echo("\nTop 5 profiles by score:")
            for p in s["top_profiles"][:5]:
                typer.echo(
                    f"  [{p['score_total']:.2f}] {p['full_name']} "
                    f"— {p['headline']} ({p['profile_category']})"
                )

    asyncio.run(_show_stats())



if __name__ == "__main__":
    app()
