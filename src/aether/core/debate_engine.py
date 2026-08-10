"""Multi-model debate — models talk over a shared transcript.

Not a free chatroom: orchestrator runs structured rounds with caps.
When the transcript approaches context limits, it is summarized and
debate continues in a new session segment (same continuity rules as chat).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from aether.core.chat_session import ChatMessage, ChatRouter, ChatSession
from aether.domain.models import ExecutionRequest


class DebateEngine:
    def __init__(self, app: Any, chat_router: ChatRouter) -> None:
        self.app = app
        self.chat = chat_router
        self.max_rounds = app.settings.orchestrator.debate_max_rounds
        self.max_agents = app.settings.orchestrator.debate_max_agents

    def _debate_providers(self) -> list[str]:
        ids: list[str] = []
        for cfg in self.app.registry.list_configs():
            if not cfg.enabled:
                if not (cfg.for_testing and cfg.provider_id == "mock"):
                    continue
            if self.app.registry.get(cfg.provider_id):
                ids.append(cfg.provider_id)
        # diversify: prefer unique families
        ids.sort(key=lambda p: (0 if p != "mock" else 1, p))
        return ids[: self.max_agents]

    async def run(
        self,
        topic: str,
        *,
        providers: Optional[list[str]] = None,
        rounds: Optional[int] = None,
        session: Optional[ChatSession] = None,
    ) -> dict[str, Any]:
        """
        Round structure:
          R1: each model proposes independently
          R2..N: each model critiques others + revises
          Final: synthesizer (first available) produces consensus note
        """
        rounds = rounds or self.max_rounds
        agents = providers or self._debate_providers()
        if len(agents) < 2:
            # pad with mock if only one real
            if "mock" in [c.provider_id for c in self.app.registry.list_configs()]:
                if "mock" not in agents:
                    agents.append("mock")
        if not agents:
            return {"ok": False, "error": "Need at least one provider for debate", "transcript": []}

        session = session or self.chat.create_session(
            provider_id=agents[0],
            mode="debate",
            title=f"Debate: {topic[:60]}",
        )
        session.mode = "debate"
        session.participating_providers = list(agents)
        session.append(
            ChatMessage(
                role="system",
                content=f"[DEBATE START] Topic: {topic}\n"
                f"Agents: {', '.join(agents)}\n"
                f"Max rounds: {rounds}\n"
                "Rules: cite reasoning; challenge assumptions; no empty agreement.",
                meta={"event": "debate_start"},
            )
        )
        self.chat.store.save(session)

        transcript: list[dict[str, Any]] = []
        ws = str(self.app.settings.workspace_root / "_debates" / session.session_id)
        Path(ws).mkdir(parents=True, exist_ok=True)

        for rnd in range(1, rounds + 1):
            session.append(
                ChatMessage(
                    role="system",
                    content=f"[DEBATE ROUND {rnd}/{rounds}]",
                    meta={"event": "debate_round", "round": rnd},
                )
            )
            for agent in agents:
                # compact if needed between speakers
                await self.chat.maybe_compact(session)

                prior = self._transcript_block(session)
                if rnd == 1:
                    instruction = (
                        f"You are {agent} in a multi-model debate.\n"
                        f"TOPIC: {topic}\n\n"
                        "Give your independent position and plan. Be concrete.\n"
                        "Do not pretend to be other models."
                    )
                else:
                    instruction = (
                        f"You are {agent} in round {rnd} of a multi-model debate.\n"
                        f"TOPIC: {topic}\n\n"
                        "Read other models' positions. Critique weaknesses, "
                        "note agreements, and revise your position.\n\n"
                        f"TRANSCRIPT SO FAR:\n{prior}"
                    )

                session.active_provider = agent
                adapter = self.app.registry.get(agent)
                if not adapter:
                    note = f"[{agent}] unavailable this round"
                    session.append(ChatMessage(role="system", content=note, provider_id=agent))
                    transcript.append({"round": rnd, "agent": agent, "text": note, "ok": False})
                    continue

                # Use chat router path for failover/compaction on each speech
                result = await self.chat.chat(
                    session,
                    instruction,
                    workspace=ws,
                    max_failovers=2,
                )
                # chat() already appended user+assistant; for debate the "user" is orchestrator prompt
                text = result.get("reply") or result.get("error") or ""
                transcript.append(
                    {
                        "round": rnd,
                        "agent": result.get("provider_id") or agent,
                        "text": text[:4000],
                        "ok": result.get("ok", False),
                        "events": result.get("events") or [],
                    }
                )

        # Synthesis
        await self.chat.maybe_compact(session)
        synth_provider = agents[0]
        session.active_provider = synth_provider
        synth_prompt = (
            f"You are the debate synthesizer ({synth_provider}).\n"
            f"TOPIC: {topic}\n\n"
            "Produce a FINAL CONSENSUS brief:\n"
            "1) Points of agreement\n"
            "2) Remaining disagreements (preserve them)\n"
            "3) Recommended decision / next actions\n"
            "4) Confidence (low/med/high) with reasons\n\n"
            f"FULL DEBATE TRANSCRIPT:\n{self._transcript_block(session)}\n"
        )
        final = await self.chat.chat(session, synth_prompt, workspace=ws, max_failovers=3)
        session.append(
            ChatMessage(
                role="system",
                content="[DEBATE END]",
                meta={"event": "debate_end"},
            )
        )
        self.chat.store.save(session)

        return {
            "ok": True,
            "session_id": session.session_id,
            "topic": topic,
            "agents": agents,
            "rounds": rounds,
            "transcript": transcript,
            "synthesis": final.get("reply") if final.get("ok") else final.get("error"),
            "session": session,
            "events": final.get("events") or [],
        }

    def _transcript_block(self, session: ChatSession, max_chars: int = 12000) -> str:
        lines: list[str] = []
        if session.memory_summary:
            lines.append(f"[SUMMARY]\n{session.memory_summary}\n")
        for m in session.current_messages():
            if m.role == "system" and m.meta.get("event") == "compacted":
                continue
            who = m.provider_id or m.role
            lines.append(f"{who}: {m.content[:2000]}")
        text = "\n\n".join(lines)
        if len(text) > max_chars:
            return text[-max_chars:]
        return text
