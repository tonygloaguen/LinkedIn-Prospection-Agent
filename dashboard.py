"""LinkedIn Prospection Agent — Rich CLI Dashboard."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

app = typer.Typer(
    name="dashboard", help="LinkedIn Prospection Agent — Rich Dashboard", add_completion=False
)
console = Console()


def _load_env() -> None:
    """Load .env file if present."""
    env_path = Path(".env")
    if env_path.exists():
        try:
            from dotenv import load_dotenv  # type: ignore[import]

            load_dotenv(env_path)
        except ImportError:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    if key.strip() not in os.environ:
                        os.environ[key.strip()] = value.strip()


async def _fetch_stats(db_path: str) -> dict[str, Any]:
    """Fetch aggregated statistics from the database.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Statistics dictionary.
    """
    import aiosqlite

    from storage.queries import get_stats

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        return await get_stats(db)


async def _fetch_run_history(db_path: str, limit: int = 10) -> list[dict[str, Any]]:
    """Fetch recent run history from the database.

    Args:
        db_path: Path to the SQLite database.
        limit: Maximum number of runs to return.

    Returns:
        List of run history dicts.
    """
    import json

    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM run_history ORDER BY started_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if d.get("metrics"):
                    d["metrics"] = json.loads(d["metrics"])
                result.append(d)
            return result


def _render_overview_panel(stats: dict[str, Any], db_path: str) -> Panel:
    """Render the overview statistics panel.

    Args:
        stats: Statistics dictionary from get_stats.
        db_path: Database path for display.

    Returns:
        Rich Panel.
    """
    max_inv = int(os.environ.get("MAX_INVITATIONS_PER_DAY", "15"))
    max_act = int(os.environ.get("MAX_ACTIONS_PER_DAY", "40"))

    inv_today = stats.get("invitations_today", 0)
    act_today = stats.get("actions_today", 0)

    text = Text()
    text.append(f"Database: {db_path}\n", style="dim")
    text.append(f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n\n", style="dim")

    text.append("Total profiles:       ", style="bold")
    text.append(f"{stats.get('profiles_total', 0)}\n", style="cyan")

    text.append("Invitations today:    ", style="bold")
    inv_color = "red" if inv_today >= max_inv else "green"
    text.append(f"{inv_today}/{max_inv}\n", style=inv_color)

    text.append("Actions today:        ", style="bold")
    act_color = "red" if act_today >= max_act else "green"
    text.append(f"{act_today}/{max_act}\n", style=act_color)

    text.append("Invitations total:    ", style="bold")
    text.append(f"{stats.get('invitations_total', 0)}\n", style="cyan")

    return Panel(text, title="[bold blue]Overview[/bold blue]", box=box.ROUNDED)


def _render_category_table(stats: dict[str, Any]) -> Table:
    """Render profile breakdown by category.

    Args:
        stats: Statistics dictionary.

    Returns:
        Rich Table.
    """
    table = Table(title="Profiles by Category", box=box.SIMPLE_HEAVY)
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Bar", no_wrap=True)

    by_cat = stats.get("by_category", {})
    total = sum(by_cat.values()) or 1

    category_colors = {
        "recruiter": "blue",
        "technical": "green",
        "cto_ciso": "magenta",
        "other": "yellow",
        None: "dim",
    }

    for cat in ["recruiter", "technical", "cto_ciso", "other", None]:
        count = by_cat.get(cat, 0)
        if count == 0:
            continue
        bar_len = max(1, int(count / total * 30))
        color = category_colors.get(cat, "white")
        table.add_row(
            f"[{color}]{cat or 'unknown'}[/{color}]",
            str(count),
            f"[{color}]{'█' * bar_len}[/{color}]",
        )

    return table


def _render_status_table(stats: dict[str, Any]) -> Table:
    """Render profile breakdown by status.

    Args:
        stats: Statistics dictionary.

    Returns:
        Rich Table.
    """
    table = Table(title="Profiles by Status", box=box.SIMPLE_HEAVY)
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")

    by_status = stats.get("by_status", {})
    status_colors = {
        "pending": "yellow",
        "messaged": "blue",
        "connected": "green",
        "ignored": "dim",
    }

    for status, count in sorted(by_status.items(), key=lambda x: -x[1]):
        color = status_colors.get(status, "white")
        table.add_row(
            f"[{color}]{status or 'unknown'}[/{color}]",
            str(count),
        )

    return table


def _render_top_profiles_table(stats: dict[str, Any]) -> Table:
    """Render top profiles by total score.

    Args:
        stats: Statistics dictionary.

    Returns:
        Rich Table.
    """
    table = Table(title="Top 10 Profiles by Score", box=box.SIMPLE_HEAVY)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Headline", max_width=40, no_wrap=True)
    table.add_column("Category")
    table.add_column("Score", justify="right")

    category_colors = {
        "recruiter": "blue",
        "technical": "green",
        "cto_ciso": "magenta",
        "other": "yellow",
    }

    for i, p in enumerate(stats.get("top_profiles", []), 1):
        cat = p.get("profile_category") or "other"
        color = category_colors.get(cat, "white")
        score = p.get("score_total", 0.0)
        score_color = "green" if score >= 0.6 else "yellow" if score >= 0.3 else "red"

        table.add_row(
            str(i),
            p.get("full_name") or "—",
            p.get("headline") or "—",
            f"[{color}]{cat}[/{color}]",
            f"[{score_color}]{score:.3f}[/{score_color}]",
        )

    return table


def _render_run_history_table(runs: list[dict[str, Any]]) -> Table:
    """Render recent run history.

    Args:
        runs: List of run history dicts.

    Returns:
        Rich Table.
    """
    table = Table(title="Recent Runs", box=box.SIMPLE_HEAVY)
    table.add_column("Date", style="dim")
    table.add_column("Posts", justify="right")
    table.add_column("Profiles", justify="right")
    table.add_column("Scored", justify="right")
    table.add_column("Invites", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Duration")

    for run in runs[:10]:
        metrics = run.get("metrics") or {}
        started_at = run.get("started_at", "")
        ended_at = run.get("ended_at", "")

        # Format date
        try:
            dt = datetime.fromisoformat(started_at)
            date_str = dt.strftime("%m-%d %H:%M")
        except Exception:
            date_str = started_at[:16] if started_at else "—"

        # Duration
        duration = "—"
        try:
            start = datetime.fromisoformat(started_at)
            end = datetime.fromisoformat(ended_at)
            secs = int((end - start).total_seconds())
            duration = f"{secs // 60}m{secs % 60:02d}s"
        except Exception:
            pass

        errors = metrics.get("errors_count", 0)
        err_color = "red" if errors > 0 else "green"

        table.add_row(
            date_str,
            str(metrics.get("posts_found", 0)),
            str(metrics.get("profiles_extracted", 0)),
            str(metrics.get("profiles_scored", 0)),
            str(metrics.get("invitations_sent", 0)),
            f"[{err_color}]{errors}[/{err_color}]",
            duration,
        )

    return table


@app.command()
def show(
    db_path: Annotated[
        str,
        typer.Option("--db", help="Path to SQLite database"),
    ] = "",
) -> None:
    """Display the full LinkedIn Prospection Agent dashboard.

    Shows overview stats, profile breakdowns by category and status,
    top scored profiles, and recent run history.
    """
    _load_env()

    path = db_path or os.environ.get("DB_PATH", "./data/linkedin.db")

    if not Path(path).exists():
        console.print(
            f"[red]No database found at {path}[/red]\nRun [bold]python main.py run[/bold] first."
        )
        raise typer.Exit(1)

    console.print()
    console.rule("[bold blue]LinkedIn Prospection Agent — Dashboard[/bold blue]")
    console.print()

    stats = asyncio.run(_fetch_stats(path))
    runs = asyncio.run(_fetch_run_history(path))

    console.print(_render_overview_panel(stats, path))
    console.print()

    # Side by side category + status
    from rich.columns import Columns

    console.print(Columns([_render_category_table(stats), _render_status_table(stats)]))
    console.print()

    console.print(_render_top_profiles_table(stats))
    console.print()

    if runs:
        console.print(_render_run_history_table(runs))
    else:
        console.print("[dim]No run history yet.[/dim]")

    console.print()


if __name__ == "__main__":
    app()
