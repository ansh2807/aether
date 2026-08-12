# AETHER — orchestrator + API + dashboard in one image.
#
#   docker compose up --build
#
# The token is printed on first start; the dashboard link includes it.
#
# Note what this image deliberately does NOT do: CLI-agent providers
# (claude, codex, gemini, opencode) drive binaries already authenticated on a
# real machine. None of that exists in a container, so a containerised AETHER
# is an API-provider deployment. `aether models` will show every CLI as not
# ready, which is the truth rather than a packaging failure.

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS dashboard
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build


FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
LABEL org.opencontainers.image.title="AETHER" \
      org.opencontainers.image.description="A deterministic orchestrator for multi-agent AI work" \
      org.opencontainers.image.source="https://github.com/ansh2807/aether" \
      org.opencontainers.image.licenses="Apache-2.0"

# git is a real dependency, not a convenience: checkpoints and the per-agent
# worktrees both shell out to it.
RUN apk add --no-cache git

WORKDIR /app
COPY pyproject.toml hatch_build.py README.md LICENSE ./
COPY src/ ./src/
COPY config/ ./config/
COPY --from=dashboard /web/dist ./web/dist
RUN pip install --no-cache-dir --require-hashes -r config/verifier-requirements.txt \
    && pip install --no-cache-dir .

# Run as a non-root user. Verification executes agent-written code, and while
# it is scrubbed and sandboxed, root inside the container is a needless
# second chance for anything that gets past that.
RUN adduser --disabled-password --gecos "" --uid 10001 aether \
    && mkdir -p /data /workspace \
    && chown -R aether:aether /data /workspace /app
USER aether

# Persist these: /data is the control plane (tasks, checkpoints, the token),
# /workspace is what the agents actually write.
VOLUME ["/data", "/workspace"]
ENV AETHER_DATA_DIR=/data \
    AETHER_WORKSPACE_ROOT=/workspace \
    AETHER_DATABASE_URL=sqlite+aiosqlite:////data/aether.db \
    PYTHONUNBUFFERED=1

EXPOSE 7420

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7420/api/health', timeout=3).status==200 else 1)"

# 0.0.0.0 inside the container only — compose.yaml binds the published port to
# host loopback. The published address is what limits reach.
# Auth stays on; see docs/DEPLOY.md before exposing this beyond localhost.
CMD ["aether", "serve", "--host", "0.0.0.0", "--port", "7420"]
