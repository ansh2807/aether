# Production deployment

AETHER can execute provider CLIs, accept provider keys, and run code produced by
agents. Treat it as a privileged single-tenant control plane, not as a public web
application. It intentionally has no multi-user authorization model.

For a local container deployment, start with `docker compose up --build`. The
bundled profile is loopback-only and applies the runtime restrictions described
below. Treat any change to its published address as a production exposure.

## Required production baseline

- Keep `server.require_auth: true` and use a randomly generated token of at least
  32 characters. Inject it as `AETHER_API_TOKEN`; do not bake it into an image.
- Terminate TLS at a reverse proxy. Forward only to AETHER on a private network,
  and restrict the public route by VPN, identity-aware proxy, or source network.
- Set `server.secure_cookies: true` when TLS terminates at that proxy so the
  dashboard session cookie is never sent over an external plaintext connection.
- Persist both `/data` and `/workspace`. Back up `/data`, including the SQLite
  database and state files, and test restoration before relying on recovery.
- Keep `security.verification_sandbox: container` and
  `security.verification_allow_fallback: false` for untrusted output. A missing
  Docker/Podman runtime then fails verification without running code on the host.
- Build and scan the default `aether:local` image before native operation. For
  a registry deployment or tests requiring extra dependencies, set
  `security.verification_image` to the immutable digest of a dedicated image;
  never enable verifier networking while an untrusted workspace is mounted.
- Never mount the host Docker socket into the AETHER application container. That
  socket is equivalent to root access to the host. Run verification workers on a
  separately isolated machine or use trusted-workspace subprocess mode.
- Run one AETHER orchestrator per data directory. Postgres storage is supported,
  but the execution queue and several control-plane state files remain
  single-host; active-active deployment is not supported.
- Run the container as the bundled non-root user and keep its root filesystem
  read-only where the platform permits, with only `/data` and `/workspace`
  writable.

## Reverse proxy

Preserve WebSocket upgrade headers for `/ws`, set a conservative request-body
limit, and disable proxy caching for `/api/` and `/v1/`. AETHER emits CSP,
anti-framing, no-referrer, and `Cache-Control: no-store` headers; the proxy should
preserve them. Configure the external dashboard origin in `server.cors_origins`
only when the dashboard is actually served from another origin.

The dashboard bootstrap link uses `#token=...`. URL fragments are not sent to
the proxy. The dashboard exchanges the value for an eight-hour HttpOnly,
SameSite cookie and removes the fragment from browser history.

## Secrets and logs

Provider secrets belong in environment variables or the local `data/.env` file
created by onboarding. Protect the data directory with OS permissions. Avoid
debug logging in production and ship `data/audit/audit.jsonl` to storage with
restricted access; prompts and task metadata may be sensitive even after secret
redaction.

## Health, shutdown, and recovery

Probe `GET /api/health` for liveness. Use authenticated `GET /api/doctor` for a
deeper readiness check. Give the process time to shut down cleanly before
forcing termination. After a restart, run:

```bash
aether doctor
aether doctor --durability
```

Exercise export/import and backup restoration in a staging environment. A green
unit suite proves code paths; it does not prove that your storage, container
runtime, reverse proxy, provider credentials, and filesystem permissions are
correctly deployed.

The bundled Docker image intentionally has no access to a container runtime.
With the production default it therefore fails verification closed. If every
task workspace is trusted and the application container itself is your accepted
isolation boundary, set `AETHER_SECURITY__VERIFICATION_SANDBOX=subprocess`.
Do not make that trade for untrusted repositories.
