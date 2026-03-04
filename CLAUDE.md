# LinkedIn Prospection Agent — CLAUDE.md

## Stack

- Python 3.11+, async/await
- LangGraph (StateGraph + TypedDict state)
- Playwright (async API, headless)
- SQLite via aiosqlite
- LLM: Gemini API (gemini-2.0-flash)
- Logging: structlog (JSON)
- Config: pydantic-settings + .env

## Key Rules

- Never use `print()` — always `logger.info/warning/error`
- No Ollama, no local model — Gemini API only
- `MAX_CONCURRENT=1` — sequential Playwright pipeline
- `headless=True` mandatory (RPi 4 deployment)
- Retry with tenacity on all Playwright + LLM calls

## Run

```bash
cp .env.example .env
# Fill in LINKEDIN_EMAIL, LINKEDIN_PASSWORD, GEMINI_API_KEY
poetry install
playwright install chromium
python main.py run --keywords "LangGraph agent" "DevSecOps NIS2"
python main.py dry-run
python main.py stats
python dashboard.py
```

## Structure

See README or project tree for full structure.
All nodes are in `agent/nodes/`, all Playwright helpers in `playwright_linkedin/`.
