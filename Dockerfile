# ──────────────────────────────────────────────────────────────────────────────
# LinkedIn Prospection Agent — Dockerfile
# Target: Raspberry Pi 4 (ARM64 / linux/arm64)
# ──────────────────────────────────────────────────────────────────────────────

# Build stage — install deps with Poetry
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=2.3.2 \
    POETRY_HOME=/opt/poetry \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /app
COPY pyproject.toml poetry.lock* ./
RUN poetry install --without dev --no-root

# ──────────────────────────────────────────────────────────────────────────────
# Runtime stage
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Playwright browsers path
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # App defaults (overridden by .env / docker-compose)
    DB_PATH=/data/linkedin.db \
    SESSION_PATH=/data/session.json \
    LOG_FILE=/logs/agent.log \
    LOG_LEVEL=INFO \
    DRY_RUN=false \
    MAX_INVITATIONS_PER_DAY=15 \
    MAX_ACTIONS_PER_DAY=40

# System dependencies required by Chromium on ARM64
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium core
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libxshmfence1 \
    # Fonts for realistic rendering
    fonts-liberation \
    fonts-noto-color-emoji \
    # Networking
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy virtualenv from builder
COPY --from=builder /app/.venv /app/.venv

# playwright-stealth and setuptools are installed by poetry (see pyproject.toml).
# pkg_resources polyfill in browser.py handles the setuptools >= 71 API change.

ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Copy project source
COPY agent/       ./agent/
COPY models/      ./models/
COPY storage/     ./storage/
COPY utils/       ./utils/
COPY playwright_linkedin/ ./playwright_linkedin/
COPY prompts/     ./prompts/
COPY main.py      ./main.py
COPY dashboard.py ./dashboard.py

# Install Playwright Chromium (ARM64 build)
RUN playwright install chromium --with-deps 2>/dev/null || \
    playwright install chromium

# Create runtime directories
RUN mkdir -p /data /logs/screenshots

# Non-root user for security
RUN useradd -u 1001 -g root -s /sbin/nologin -M agent && \
    chown -R agent:root /app /data /logs /ms-playwright 2>/dev/null || true
USER agent

# Health check — verify Python + imports work
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "from agent.graph import run_pipeline; print('ok')" || exit 1

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
