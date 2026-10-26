# ---------------------------------------------------------------------------
# Dockerfile = the recipe for building an IMAGE (a frozen, read-only filesystem
# with Python + our code + dependencies). A CONTAINER is a running copy of it.
# Each instruction below creates a cached "layer"; Docker only re-runs the
# steps whose inputs changed.
# ---------------------------------------------------------------------------

# Start from the official slim Python image. It is multi-architecture, so on
# your Oracle ARM (aarch64) VPS Docker automatically pulls the ARM variant.
FROM python:3.13-slim

# Don't write .pyc files, print logs immediately, keep pip from caching downloads (smaller image).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# All following commands run inside /app in the image.
WORKDIR /app

# Copy only what's needed to install (see .dockerignore for what's left out).
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# Never run as root inside the container: create an unprivileged user that owns /data.
RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data
USER app

# Defaults; anything in your .env file overrides these.
ENV DATA_DIR=/data \
    FASTMCP_HOME=/data/fastmcp \
    HOST=0.0.0.0 \
    PORT=8000 \
    TZ=America/Sao_Paulo

# Documentation only: the app listens on 8000 *inside* the container network.
EXPOSE 8000

# Docker pings this every 30s; `docker ps` shows "healthy" / "unhealthy".
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

# The command that runs when a container starts.
CMD ["python", "-m", "finance_mcp"]
