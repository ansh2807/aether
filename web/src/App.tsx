import React, { useCallback, useEffect, useMemo, useState } from "react";

type Task = {
  task_id: string;
  status: string;
  progress: number;
  normalized_goal: string;
  active_agent?: string | null;
  participating_agents?: string[];
  mode?: string;
  final_output?: string;
  failure_reason?: string;
};

type ProviderHealth = {
  provider_id: string;
  available: boolean;
  availability: string;
  message: string;
  latency_ms?: number | null;
};

type ConnectInfo = {
  provider_id: string;
  name: string;
  kind: string;
  connect?: string;
  connected?: boolean;
  has_key?: boolean;
  key_url?: string;
  api_key_env?: string;
  claude?: {
    loggedIn?: boolean;
    email?: string;
    authMethod?: string;
    subscriptionType?: string;
    orgName?: string;
  };
};

type CatalogEntry = {
  id: string;
  name: string;
  family?: string;
  kind: string;
  connect?: string;
  models?: string[];
  multi_model?: boolean;
  blurb?: string;
  api_key_env?: string;
  key_url?: string;
};

type ConnectOverview = {
  claude: {
    loggedIn: boolean;
    email?: string;
    authMethod?: string;
    subscriptionType?: string;
    orgName?: string;
  };
  providers: ConnectInfo[];
  catalog?: CatalogEntry[];
  profile?: {
    mode?: string;
    selected_providers?: string[];
    models?: Record<string, string>;
    opencode_models?: string[];
  };
  opencode_models_fallback?: string[];
};

type Approval = {
  approval_id: string;
  task_id: string;
  operation: string;
  detail: string;
  risk: string;
  status: string;
};

type Inspect = {
  task: Task;
  memory: any;
  timeline: Array<{ timestamp: string; actor: string; action: string; detail: string }>;
  messages: Array<{ sender: string; message_type: string; content: string; timestamp: string }>;
  handoffs: any[];
  checkpoints: any[];
  verifications: any[];
  budget?: any;
  trace?: any[];
  pending_approvals?: Approval[];
};

const API = "";

// `aether serve` puts a one-time bootstrap value in the URL fragment. Fragments
// are not sent in HTTP requests. Exchange it for an HttpOnly SameSite cookie;
// never persist the bearer token in JavaScript-readable storage.
function takeBootstrapToken(): string {
  // Remove credentials left by pre-hardening dashboard releases.
  try {
    localStorage.removeItem("aether.apiToken");
  } catch {
    /* storage may be disabled; there is still nothing to persist */
  }
  const hash = new URLSearchParams(location.hash.replace(/^#/, "")).get("token") || "";
  // Accept old links once, then immediately scrub the query string.
  const legacy = new URLSearchParams(location.search).get("token") || "";
  if (hash || legacy) {
    const url = new URL(location.href);
    url.hash = "";
    url.searchParams.delete("token");
    history.replaceState({}, "", url.toString());
  }
  return hash || legacy;
}

let bootstrapToken = takeBootstrapToken();

function authHeaders(): Record<string, string> {
  return bootstrapToken ? { Authorization: `Bearer ${bootstrapToken}` } : {};
}

async function establishDashboardSession(): Promise<void> {
  if (!bootstrapToken) return;
  const res = await fetch("/api/auth/session", {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Dashboard bootstrap token is invalid or expired.");
  bootstrapToken = "";
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(init?.headers || {}),
    },
    ...init,
  });
  if (!res.ok) {
    if (res.status === 401) {
      throw new Error(
        "API token missing or invalid. Open the dashboard link printed by `aether serve`.",
      );
    }
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

function statusColor(status: string): string {
  if (status === "COMPLETED") return "ok";
  if (status === "FAILED" || status === "CANCELLED") return "err";
  if (status === "WAITING") return "warn";
  return "";
}

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [health, setHealth] = useState<ProviderHealth[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [metrics, setMetrics] = useState<any>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [inspect, setInspect] = useState<Inspect | null>(null);
  const [streamLog, setStreamLog] = useState<string[]>([]);
  const [prompt, setPrompt] = useState("Build a production-ready QR-based file transfer application");
  const [mode, setMode] = useState("solo");
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [connectInfo, setConnectInfo] = useState<ConnectOverview | null>(null);
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [connectMsg, setConnectMsg] = useState<string | null>(null);
  const [ocModels, setOcModels] = useState<string[]>([]);
  const [ocSelected, setOcSelected] = useState<string[]>([]);
  const [chatSessionId, setChatSessionId] = useState<string | null>(null);
  const [chatLog, setChatLog] = useState<Array<{ role: string; text: string; provider?: string }>>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatStreaming, setChatStreaming] = useState(false);
  const [chatUsage, setChatUsage] = useState<any>(null);
  const [chatProvider, setChatProvider] = useState<string>("");
  const [authReady, setAuthReady] = useState(false);

  useEffect(() => {
    establishDashboardSession()
      .catch((e: Error) => setError(e.message))
      .finally(() => setAuthReady(true));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [t, h, a, m, c] = await Promise.all([
        api<Task[]>("/api/tasks?limit=50"),
        api<ProviderHealth[]>("/api/agents/health"),
        api<Approval[]>("/api/approvals"),
        api<any>("/api/metrics"),
        api<ConnectOverview>("/api/connect").catch(() => null),
      ]);
      setTasks(t);
      setHealth(h);
      setApprovals(a);
      setMetrics(m);
      if (c) {
        setConnectInfo(c);
        if (c.profile?.opencode_models?.length) {
          setOcSelected(c.profile.opencode_models);
        }
      }
      setError(null);
    } catch (e: any) {
      setError(e.message || "API unreachable — start with `aether serve`");
    }
  }, []);

  const loadInspect = useCallback(async (id: string) => {
    try {
      const data = await api<Inspect>(`/api/tasks/${id}`);
      setInspect(data);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    if (!authReady) return;
    refresh();
    const id = setInterval(refresh, 2500);
    return () => clearInterval(id);
  }, [refresh, authReady]);

  useEffect(() => {
    if (selected) loadInspect(selected);
  }, [selected, loadInspect, tasks]);

  useEffect(() => {
    if (!authReady) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data.type === "agent_stream" && data.text) {
          setStreamLog((prev) => [...prev.slice(-40), `[${data.provider}] ${data.text.slice(0, 200)}`]);
        }
        if (data.type === "approval_required") {
          refresh();
        }
      } catch {
        /* ignore */
      }
      refresh();
      if (selected) loadInspect(selected);
    };
    const ping = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, 15000);
    return () => {
      clearInterval(ping);
      ws.close();
    };
  }, [refresh, loadInspect, selected, authReady]);

  const activeTasks = useMemo(
    () =>
      tasks.filter((t) =>
        ["RUNNING", "PLANNING", "QUEUED", "HANDOFF", "TESTING", "REVIEW", "WAITING"].includes(t.status)
      ),
    [tasks]
  );

  async function submitTask() {
    if (!prompt.trim()) return;
    setBusy(true);
    try {
      const t = await api<Task>("/api/tasks", {
        method: "POST",
        body: JSON.stringify({ prompt, mode, auto_start: true }),
      });
      setSelected(t.task_id);
      setStreamLog([]);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function act(path: string) {
    if (!selected) return;
    setBusy(true);
    try {
      await api(`/api/tasks/${selected}/${path}`, { method: "POST" });
      await refresh();
      await loadInspect(selected);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function resolveApproval(id: string, granted: boolean) {
    setBusy(true);
    try {
      await api(`/api/approvals/${id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ granted, auto_resume: true }),
      });
      await refresh();
      if (selected) await loadInspect(selected);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function connectProvider(providerId: string, force = false) {
    setBusy(true);
    setConnectMsg(null);
    try {
      const res = await api<{ ok: boolean; message: string; method: string }>(
        `/api/connect/${providerId}`,
        {
          method: "POST",
          body: JSON.stringify({ force }),
        }
      );
      setConnectMsg(
        res.ok
          ? `✓ ${providerId}: ${res.message}`
          : `○ ${providerId}: ${res.message}`
      );
      // Claude OAuth may finish a few seconds later
      setTimeout(() => refresh(), 1500);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveProviderKey(providerId: string) {
    const key = (keyDrafts[providerId] || "").trim();
    if (!key) {
      setConnectMsg("Paste an API key first");
      return;
    }
    setBusy(true);
    setConnectMsg(null);
    try {
      const res = await api<{ ok: boolean; message: string }>(`/api/connect/${providerId}/key`, {
        method: "POST",
        body: JSON.stringify({ api_key: key }),
      });
      setKeyDrafts((d) => ({ ...d, [providerId]: "" }));
      setConnectMsg(`✓ ${providerId}: ${res.message}`);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const claudeConnected = !!connectInfo?.claude?.loggedIn;

  async function ensureChatSession() {
    if (chatSessionId) return chatSessionId;
    const s = await api<{ session_id: string; active_provider?: string }>("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify({ title: "Dashboard chat", mode: "chat" }),
    });
    setChatSessionId(s.session_id);
    setChatProvider(s.active_provider || "");
    return s.session_id;
  }

  async function sendChat() {
    const text = chatInput.trim();
    if (!text || chatStreaming) return;
    setChatInput("");
    setChatLog((L) => [...L, { role: "user", text }]);
    setChatStreaming(true);
    let sid = chatSessionId;
    try {
      sid = await ensureChatSession();
      // Prefer SSE stream
      const res = await fetch(`/api/chat/sessions/${sid}/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ message: text, stream: true }),
      });
      if (!res.ok || !res.body) {
        // fallback non-stream
        const full = await api<any>(`/api/chat/sessions/${sid}/messages`, {
          method: "POST",
          body: JSON.stringify({ message: text, stream: false }),
        });
        setChatLog((L) => [
          ...L,
          {
            role: "assistant",
            text: full.reply || full.error || "(no reply)",
            provider: full.provider_id,
          },
        ]);
        setChatUsage(full.usage || null);
        setChatProvider(full.provider_id || "");
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = "";
      let provider = "";
      let buf = "";
      setChatLog((L) => [...L, { role: "assistant", text: "", provider: "…" }]);
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const block of parts) {
          const line = block.trim();
          if (!line.startsWith("data: ")) continue;
          try {
            const ev = JSON.parse(line.slice(6));
            if (ev.type === "token" && ev.text) {
              acc += ev.text;
              provider = ev.provider || provider;
              setChatLog((L) => {
                const copy = [...L];
                const last = copy[copy.length - 1];
                if (last?.role === "assistant") {
                  copy[copy.length - 1] = { role: "assistant", text: acc, provider };
                }
                return copy;
              });
            } else if (ev.type === "event") {
              const name = String(ev.event || "");
              if (name.includes("compact") || name.includes("switch") || name.includes("failover")) {
                setChatLog((L) => [...L, { role: "system", text: name }]);
              }
              if (ev.provider) provider = ev.provider;
            } else if (ev.type === "done") {
              setChatUsage(ev.usage || null);
              setChatProvider(ev.provider_id || provider);
              if (ev.ok === false) {
                setChatLog((L) => [...L, { role: "system", text: ev.error || "failed" }]);
              }
            }
          } catch {
            /* ignore partial json */
          }
        }
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setChatStreaming(false);
    }
  }

  async function compactChat() {
    if (!chatSessionId) return;
    setBusy(true);
    try {
      const r = await api<any>(`/api/chat/sessions/${chatSessionId}/compact`, { method: "POST" });
      setConnectMsg(
        r.ok
          ? `Session compacted → segment ${r.segment}`
          : "Nothing to compact"
      );
      setChatLog((L) => [...L, { role: "system", text: `compacted → segment ${r.segment}` }]);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function newChatSession() {
    setBusy(true);
    try {
      const s = await api<{ session_id: string; active_provider?: string }>("/api/chat/sessions", {
        method: "POST",
        body: JSON.stringify({ title: "Dashboard chat", mode: "chat" }),
      });
      setChatSessionId(s.session_id);
      setChatProvider(s.active_provider || "");
      setChatLog([]);
      setChatUsage(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function loadOpenCodeModels() {
    setBusy(true);
    try {
      const res = await api<{ models: string[]; installed: boolean; message?: string }>(
        "/api/connect/opencode/models"
      );
      setOcModels(res.models || []);
      setConnectMsg(
        res.installed
          ? `Open Code: ${res.models?.length || 0} models loaded`
          : res.message || "Open Code not installed — showing fallback list"
      );
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  function toggleOcModel(id: string) {
    setOcSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  async function saveOpenCodeSelection() {
    if (!ocSelected.length) {
      setConnectMsg("Pick at least one Open Code model");
      return;
    }
    setBusy(true);
    try {
      const res = await api<{ ok: boolean; opencode_models: string[] }>(
        "/api/connect/opencode/select",
        {
          method: "POST",
          body: JSON.stringify({ models: ocSelected }),
        }
      );
      setConnectMsg(`✓ Open Code multi-model saved: ${res.opencode_models.join(", ")}`);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const platformOrder = ["claude_cli", "gemini", "grok", "kimi", "opencode_cli"];
  const apiPlatforms = (connectInfo?.providers || []).filter((p) =>
    ["gemini", "grok", "kimi", "openai"].includes(p.provider_id)
  );
  const ocProvider = (connectInfo?.providers || []).find((p) => p.provider_id === "opencode_cli");
  const displayOcModels =
    ocModels.length > 0
      ? ocModels
      : connectInfo?.opencode_models_fallback || [];

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          <h1>AETHER</h1>
          <p>Universal Multi-Agent AI Orchestrator — deterministic control plane</p>
        </div>
        <div className="row">
          {metrics?.tasks && (
            <div className="badge">
              ✓ {metrics.tasks.completed || 0} / ✗ {metrics.tasks.failed || 0} · fo {metrics.failovers || 0}
            </div>
          )}
          <div className={`badge ${connected ? "ok" : "err"}`}>
            <span className={`dot ${connected ? "on" : "off"}`} />
            {connected ? "Live" : "Offline"}
          </div>
        </div>
      </header>

      {error && (
        <div className="card" style={{ marginBottom: 16, borderColor: "rgba(240,68,84,0.4)" }}>
          {error}
        </div>
      )}

      {approvals.length > 0 && (
        <section className="card" style={{ marginBottom: 16, borderColor: "rgba(245,165,36,0.45)" }}>
          <h2>Pending Approvals ({approvals.length})</h2>
          {approvals.map((a) => (
            <div key={a.approval_id} className="row" style={{ marginBottom: 8, borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
              <div style={{ flex: 1 }}>
                <strong>{a.operation}</strong> · {a.task_id} · risk={a.risk}
                <div style={{ color: "var(--muted)", fontSize: "0.85rem" }}>{a.detail}</div>
              </div>
              <button disabled={busy} onClick={() => resolveApproval(a.approval_id, true)}>
                Grant
              </button>
              <button className="danger" disabled={busy} onClick={() => resolveApproval(a.approval_id, false)}>
                Deny
              </button>
            </div>
          ))}
        </section>
      )}

      <section className="card" style={{ marginBottom: 16 }}>
        <h2>Connect models</h2>
        <p style={{ margin: "0 0 12px", color: "var(--muted)", fontSize: "0.9rem" }}>
          Login lineup: <strong>Claude · Google · Grok · Kimi · Open Code</strong>. Open Code is a multi-model hub
          (pick many models).
        </p>

        {connectMsg && (
          <div
            style={{
              marginBottom: 12,
              padding: "8px 10px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "var(--bg-elev)",
              fontSize: "0.9rem",
            }}
          >
            {connectMsg}
          </div>
        )}

        {/* Claude */}
        <div
          className="row"
          style={{
            alignItems: "stretch",
            marginBottom: 12,
            padding: 12,
            borderRadius: 12,
            border: claudeConnected
              ? "1px solid rgba(34,197,94,0.45)"
              : "1px solid rgba(91,140,255,0.45)",
            background: "var(--bg-elev)",
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 700, fontSize: "1.05rem" }}>Claude</div>
            <div style={{ color: "var(--muted)", fontSize: "0.85rem", marginTop: 4 }}>
              {claudeConnected ? (
                <>
                  <span className="dot on" /> Connected as{" "}
                  <strong style={{ color: "var(--text)" }}>{connectInfo?.claude?.email}</strong>
                  {connectInfo?.claude?.subscriptionType
                    ? ` · ${connectInfo.claude.subscriptionType}`
                    : ""}
                </>
              ) : (
                <>
                  <span className="dot off" /> Browser OAuth (same as Claude Code)
                </>
              )}
            </div>
          </div>
          <div className="row">
            <button disabled={busy} onClick={() => connectProvider("claude_cli", claudeConnected)}>
              {claudeConnected ? "Reconnect Claude" : "Connect Claude"}
            </button>
            <button className="secondary" disabled={busy} onClick={() => refresh()}>
              Refresh
            </button>
          </div>
        </div>

        {/* Google / Grok / Kimi / OpenAI keys */}
        <div style={{ display: "grid", gap: 10, marginBottom: 14 }}>
          {apiPlatforms.map((p) => (
            <div
              key={p.provider_id}
              style={{
                borderBottom: "1px solid var(--border)",
                paddingBottom: 10,
              }}
            >
              <div style={{ fontWeight: 600 }}>
                <span className={`dot ${p.connected ? "on" : "off"}`} />
                {p.name || p.provider_id}
              </div>
              <div style={{ color: "var(--muted)", fontSize: "0.8rem" }}>
                {p.connected ? "Key saved" : "Needs API key"} · {p.api_key_env}
              </div>
              <div className="row" style={{ marginTop: 8 }}>
                <input
                  type="password"
                  placeholder="Paste API key"
                  value={keyDrafts[p.provider_id] || ""}
                  onChange={(e) =>
                    setKeyDrafts((d) => ({ ...d, [p.provider_id]: e.target.value }))
                  }
                  style={{
                    flex: 1,
                    minWidth: 160,
                    borderRadius: 8,
                    border: "1px solid var(--border)",
                    background: "var(--bg)",
                    color: "var(--text)",
                    padding: "8px 10px",
                  }}
                />
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => connectProvider(p.provider_id)}
                >
                  Open key page
                </button>
                <button disabled={busy} onClick={() => saveProviderKey(p.provider_id)}>
                  Save key
                </button>
              </div>
            </div>
          ))}
        </div>

        {/* Open Code multi-model */}
        <div
          style={{
            padding: 12,
            borderRadius: 12,
            border: "1px solid rgba(192,132,252,0.4)",
            background: "var(--bg-elev)",
          }}
        >
          <div className="row" style={{ marginBottom: 8 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 700 }}>Open Code — multi-model</div>
              <div style={{ color: "var(--muted)", fontSize: "0.85rem" }}>
                <span className={`dot ${ocProvider?.connected ? "on" : "off"}`} />
                {ocProvider?.connected ? "CLI ready" : "Install/login Open Code"} · pick Kimi, Grok,
                Claude, Gemini, GPT…
              </div>
            </div>
            <button className="secondary" disabled={busy} onClick={() => connectProvider("opencode_cli")}>
              Connect Open Code
            </button>
            <button className="secondary" disabled={busy} onClick={loadOpenCodeModels}>
              Load models
            </button>
            <button disabled={busy || !ocSelected.length} onClick={saveOpenCodeSelection}>
              Save selection ({ocSelected.length})
            </button>
          </div>
          {ocSelected.length > 0 && (
            <div style={{ fontSize: "0.85rem", marginBottom: 8, color: "var(--accent-2)" }}>
              Selected: {ocSelected.join(", ")}
            </div>
          )}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
              gap: 6,
              maxHeight: 220,
              overflow: "auto",
            }}
          >
            {(displayOcModels.length ? displayOcModels : ["Load models to list options"]).map(
              (m) => {
                const active = ocSelected.includes(m);
                const isPlaceholder = m.startsWith("Load models");
                return (
                  <button
                    key={m}
                    type="button"
                    disabled={busy || isPlaceholder}
                    onClick={() => !isPlaceholder && toggleOcModel(m)}
                    className="secondary"
                    style={{
                      textAlign: "left",
                      fontSize: "0.78rem",
                      fontWeight: 500,
                      borderColor: active ? "rgba(34,197,94,0.6)" : undefined,
                      background: active ? "rgba(34,197,94,0.12)" : undefined,
                    }}
                  >
                    {active ? "✓ " : ""}
                    {m}
                  </button>
                );
              }
            )}
          </div>
        </div>
      </section>

      <section className="card" style={{ marginBottom: 16 }}>
        <h2>Live chat (LLM router)</h2>
        <p style={{ margin: "0 0 10px", color: "var(--muted)", fontSize: "0.9rem" }}>
          Same engine as the terminal: stream tokens · auto model-switch · auto-compact on context limit.
          {chatSessionId ? (
            <>
              {" "}
              session <code>{chatSessionId.slice(0, 16)}</code>
              {chatProvider ? ` · model ${chatProvider}` : ""}
            </>
          ) : null}
        </p>
        <div className="row" style={{ marginBottom: 8 }}>
          <button className="secondary" disabled={busy || chatStreaming} onClick={newChatSession}>
            New session
          </button>
          <button className="secondary" disabled={busy || !chatSessionId} onClick={compactChat}>
            Compact / rollover
          </button>
          {chatUsage && (
            <span style={{ color: "var(--muted)", fontSize: "0.85rem" }}>
              ctx {chatUsage.used_tokens}/{chatUsage.context_window} ({Math.round((chatUsage.ratio || 0) * 100)}
              %)
            </span>
          )}
        </div>
        <div
          style={{
            minHeight: 180,
            maxHeight: 320,
            overflow: "auto",
            border: "1px solid var(--border)",
            borderRadius: 10,
            padding: 10,
            background: "var(--bg)",
            marginBottom: 10,
            fontSize: "0.9rem",
          }}
        >
          {chatLog.length === 0 && (
            <div className="empty">Start chatting — tokens stream live like Grok / Claude Code.</div>
          )}
          {chatLog.map((m, i) => (
            <div key={i} style={{ marginBottom: 8 }}>
              <div style={{ fontWeight: 600, fontSize: "0.78rem", color: "var(--accent-2)" }}>
                {m.role}
                {m.provider ? ` · ${m.provider}` : ""}
              </div>
              <div style={{ whiteSpace: "pre-wrap" }}>{m.text}</div>
            </div>
          ))}
          {chatStreaming && <div style={{ color: "var(--muted)" }}>▍…</div>}
        </div>
        <div className="row">
          <input
            type="text"
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && sendChat()}
            placeholder="Message the active model…"
            disabled={chatStreaming}
            style={{
              flex: 1,
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "var(--bg)",
              color: "var(--text)",
              padding: "10px 12px",
            }}
          />
          <button disabled={chatStreaming || !chatInput.trim()} onClick={sendChat}>
            Send
          </button>
        </div>
      </section>

      <div className="grid">
        <div className="stack">
          <section className="card">
            <h2>Submit Task</h2>
            <div className="compose">
              <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} />
              <div className="row">
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option value="solo">solo</option>
                  <option value="collab">collab</option>
                  <option value="debate">debate</option>
                  <option value="parallel">parallel</option>
                </select>
                <button onClick={submitTask} disabled={busy}>
                  Run with AETHER
                </button>
              </div>
            </div>
          </section>

          <section className="card">
            <h2>Active Tasks ({activeTasks.length})</h2>
            <div className="task-list">
              {tasks.length === 0 && <div className="empty">No tasks yet.</div>}
              {tasks.map((t) => (
                <button
                  key={t.task_id}
                  className={`task-item ${selected === t.task_id ? "active" : ""}`}
                  onClick={() => setSelected(t.task_id)}
                >
                  <div>{t.normalized_goal || t.task_id}</div>
                  <div className="meta">
                    <span>
                      {t.task_id} · <span className={statusColor(t.status)}>{t.status}</span>
                    </span>
                    <span>{t.active_agent || "—"}</span>
                  </div>
                  <div className="progress">
                    <span style={{ width: `${Math.round((t.progress || 0) * 100)}%` }} />
                  </div>
                </button>
              ))}
            </div>
          </section>

          <section className="card">
            <h2>Task Detail</h2>
            {!inspect && <div className="empty">Select a task</div>}
            {inspect && (
              <>
                <div className="kv">
                  <div className="k">ID</div>
                  <div>{inspect.task.task_id}</div>
                  <div className="k">Status</div>
                  <div>{inspect.task.status}</div>
                  <div className="k">Progress</div>
                  <div>{Math.round((inspect.task.progress || 0) * 100)}%</div>
                  <div className="k">Mode</div>
                  <div>{inspect.task.mode}</div>
                  <div className="k">Agents</div>
                  <div>{(inspect.task.participating_agents || []).join(" → ") || "—"}</div>
                  <div className="k">Current</div>
                  <div>{inspect.task.active_agent || "—"}</div>
                  <div className="k">State</div>
                  <div>{inspect.memory?.current_state || "—"}</div>
                  <div className="k">Next</div>
                  <div>{inspect.memory?.next_action || "—"}</div>
                  {inspect.budget && (
                    <>
                      <div className="k">Budget</div>
                      <div>
                        {inspect.budget.used_tokens}/{inspect.budget.max_tokens ?? "∞"} tok ·{" "}
                        {inspect.budget.used_requests}/{inspect.budget.max_requests ?? "∞"} req · $
                        {(inspect.budget.estimated_cost_usd || 0).toFixed(4)}
                      </div>
                    </>
                  )}
                </div>
                <div className="row" style={{ marginTop: 12 }}>
                  <button className="secondary" disabled={busy} onClick={() => act("pause")}>
                    Pause
                  </button>
                  <button className="secondary" disabled={busy} onClick={() => act("resume")}>
                    Resume
                  </button>
                  <button className="danger" disabled={busy} onClick={() => act("cancel")}>
                    Cancel
                  </button>
                </div>
                {inspect.task.final_output && (
                  <pre
                    style={{
                      marginTop: 12,
                      whiteSpace: "pre-wrap",
                      fontSize: "0.8rem",
                      color: "var(--muted)",
                    }}
                  >
                    {inspect.task.final_output.slice(0, 2500)}
                  </pre>
                )}
              </>
            )}
          </section>
        </div>

        <div className="stack">
          <section className="card">
            <h2>Provider Health</h2>
            {health.length === 0 && <div className="empty">No providers reported</div>}
            {health.map((h) => (
              <div className="provider-row" key={h.provider_id}>
                <span>
                  <span className={`dot ${h.available ? "on" : h.availability.includes("RATE") ? "warn" : "off"}`} />
                  {h.provider_id}
                </span>
                <span style={{ color: "var(--muted)", fontSize: "0.8rem" }}>
                  {h.availability}
                  {h.latency_ms != null ? ` · ${Math.round(h.latency_ms)}ms` : ""}
                </span>
              </div>
            ))}
            <div className="row" style={{ marginTop: 10 }}>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => connectProvider("claude_cli", true)}
                style={{ width: "100%" }}
              >
                Connect Claude (OAuth)
              </button>
            </div>
          </section>

          <section className="card">
            <h2>Live Stream</h2>
            <div className="timeline">
              {streamLog.length === 0 && <div className="empty">Agent output appears here</div>}
              {streamLog.map((line, i) => (
                <div className="ev" key={i}>
                  {line}
                </div>
              ))}
            </div>
          </section>

          <section className="card">
            <h2>Timeline</h2>
            <div className="timeline">
              {!inspect?.timeline?.length && <div className="empty">No events</div>}
              {(inspect?.timeline || []).slice(-40).map((e, i) => (
                <div className="ev" key={i}>
                  <div>
                    {new Date(e.timestamp).toLocaleTimeString()} · <strong>{e.actor}</strong> {e.action}
                  </div>
                  <div style={{ color: "var(--muted)" }}>{e.detail}</div>
                </div>
              ))}
            </div>
          </section>

          <section className="card">
            <h2>Collaboration</h2>
            <div className="messages">
              {!inspect?.messages?.length && <div className="empty">No messages</div>}
              {(inspect?.messages || []).slice(-30).map((m, i) => (
                <div className="msg" key={i}>
                  <div className="who">
                    {m.sender} · {m.message_type}
                  </div>
                  <div>{m.content}</div>
                </div>
              ))}
            </div>
          </section>

          <section className="card">
            <h2>Checkpoints</h2>
            <div className="messages">
              {!inspect?.checkpoints?.length && <div className="empty">None</div>}
              {(inspect?.checkpoints || []).slice(-10).map((c: any, i: number) => (
                <div className="msg" key={i}>
                  <div className="who">
                    {c.checkpoint_id} · {c.agent_id || "—"} · {(c.git_commit || "").slice(0, 8)}
                  </div>
                  <div>{c.state_summary}</div>
                </div>
              ))}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
