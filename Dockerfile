# AETHER — orchestrator + API + dashboard in one image.
#
#   docker build -t aether .
#   docker run -p 7420:7420 -v aether-data:/data aether
#
# The token is printed on first start; the dashboard link includes it.
#
# Note what this image deliberately does NOT do: CLI-agent providers
# (claude, codex, gemini, opencode) drive binaries already authenticated on a
# real machine. None of that exists in a container, so a containerised AETHER
# is an API-provider deployment. `aether models` will show every CLI as not
# ready, which is the truth rather than a packaging failure.

FROM python:3.12-slim AS dashboard
WORKDIR /web
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build


FROM python:3.12-slim
LABEL org.opencontainers.image.title="AETHER" \
      org.opencontainers.image.description="A deterministic orchestrator for multi-agent AI work" \
      org.opencontainers.image.source="https://github.com/ansh2807/aether" \
      org.opencontainers.image.licenses="Apache-2.0"

# git is a real dependency, not a convenience: checkpoints and the per-agent
# worktrees both shell out to it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY config/ ./config/
COPY --from=dashboard /web/dist ./web/dist

# Run as a non-root user. Verification executes agent-written code, and while
# it is scrubbed and sandboxed, root inside the container is a needless
# second chance for anything that gets past that.
RUN useradd --create-home --uid 10001 aether \
    && mkdir -p /data /workspace \
    && chown -R aether:aether /data /workspace /app
USER aether

# Persist these: /data is the control plane (tasks, checkpoints, the token),
# /workspace is what the agents actually write.
VOLUME ["/data", "/workspace"]
ENV AETHER_DATA_DIR=/data \
    AETHER_WORKSPACE_ROOT=/workspace \
    PYTHONUNBUFFERED=1

EXPOSE 7420

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7420/api/health', timeout=3).status==200 else 1)"

# 0.0.0.0 inside the container only — the published port is what limits reach.
# Auth stays on; see docs/DEPLOY.md before exposing this beyond localhost.
CMD ["aether", "serve", "--host", "0.0.0.0", "--port", "7420"]
