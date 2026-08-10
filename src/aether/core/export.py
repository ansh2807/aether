"""Task export / import for continuity across machines."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aether.domain.enums import TaskStatus
from aether.domain.models import Task, TaskMemory
from aether.storage.repository import SqliteRepository


class TaskExporter:
    def __init__(self, repo: SqliteRepository) -> None:
        self.repo = repo

    async def export_dict(self, task_id: str) -> dict[str, Any]:
        task = await self.repo.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        memory = await self.repo.get_memory(task_id)
        return {
            "format": "aether-task-export-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "task": task.model_dump(mode="json"),
            "memory": memory.model_dump(mode="json") if memory else None,
            "timeline": [e.model_dump(mode="json") for e in await self.repo.list_events(task_id)],
            "handoffs": [h.model_dump(mode="json") for h in await self.repo.list_handoffs(task_id)],
            "checkpoints": [c.model_dump(mode="json") for c in await self.repo.list_checkpoints(task_id)],
            "messages": [m.model_dump(mode="json") for m in await self.repo.list_messages(task_id)],
            "verifications": [v.model_dump(mode="json") for v in await self.repo.list_results(task_id)],
        }

    async def export_json(self, task_id: str, path: Path) -> Path:
        data = await self.export_dict(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    async def export_bundle(self, task_id: str, path: Path, workspace: Optional[Path] = None) -> Path:
        data = await self.export_dict(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("task.json", json.dumps(data, indent=2, default=str))
            if workspace and workspace.exists():
                for file in workspace.rglob("*"):
                    if file.is_file() and ".git" not in file.parts:
                        arc = Path("workspace") / file.relative_to(workspace)
                        zf.write(file, arcname=str(arc).replace("\\", "/"))
        return path

    async def import_dict(
        self,
        data: dict[str, Any],
        as_paused: bool = True,
        skip_existing_events: bool = True,
    ) -> Task:
        if data.get("format") not in (None, "aether-task-export-v1"):
            raise ValueError(f"Unsupported export format: {data.get('format')}")
        task = Task.model_validate(data["task"])
        if as_paused and task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            task.status = TaskStatus.PAUSED
        await self.repo.save_task(task)
        if data.get("memory"):
            memory = TaskMemory.model_validate(data["memory"])
            await self.repo.save_memory(memory)
        # restore timeline / handoffs / etc. (idempotent when re-importing same export)
        from aether.domain.models import (
            AgentMessage,
            Checkpoint,
            HandoffReport,
            TimelineEvent,
            VerificationResult,
            new_id,
        )

        existing_event_ids: set[str] = set()
        existing_msg_ids: set[str] = set()
        existing_ho_ids: set[str] = set()
        existing_cp_ids: set[str] = set()
        existing_vr_ids: set[str] = set()
        if skip_existing_events:
            existing_event_ids = {e.event_id for e in await self.repo.list_events(task.task_id)}
            existing_msg_ids = {m.message_id for m in await self.repo.list_messages(task.task_id)}
            existing_ho_ids = {h.handoff_id for h in await self.repo.list_handoffs(task.task_id)}
            existing_cp_ids = {c.checkpoint_id for c in await self.repo.list_checkpoints(task.task_id)}
            existing_vr_ids = {v.result_id for v in await self.repo.list_results(task.task_id)}

        for item in data.get("timeline") or []:
            ev = TimelineEvent.model_validate(item)
            if ev.event_id in existing_event_ids:
                continue
            try:
                await self.repo.append_event(ev)
            except Exception:  # noqa: BLE001
                ev.event_id = new_id("ev-")
                await self.repo.append_event(ev)
        for item in data.get("handoffs") or []:
            ho = HandoffReport.model_validate(item)
            if ho.handoff_id in existing_ho_ids:
                continue
            try:
                await self.repo.save_handoff(ho)
            except Exception:  # noqa: BLE001
                ho.handoff_id = new_id("ho-")
                await self.repo.save_handoff(ho)
        for item in data.get("checkpoints") or []:
            cp = Checkpoint.model_validate(item)
            if cp.checkpoint_id in existing_cp_ids:
                continue
            try:
                await self.repo.save_checkpoint(cp)
            except Exception:  # noqa: BLE001
                cp.checkpoint_id = new_id("cp-")
                await self.repo.save_checkpoint(cp)
        for item in data.get("messages") or []:
            msg = AgentMessage.model_validate(item)
            if msg.message_id in existing_msg_ids:
                continue
            try:
                await self.repo.append_message(msg)
            except Exception:  # noqa: BLE001
                msg.message_id = new_id("msg-")
                await self.repo.append_message(msg)
        for item in data.get("verifications") or []:
            vr = VerificationResult.model_validate(item)
            if vr.result_id in existing_vr_ids:
                continue
            try:
                await self.repo.save_result(vr)
            except Exception:  # noqa: BLE001
                vr.result_id = new_id("vr-")
                await self.repo.save_result(vr)
        return task

    async def import_json(self, path: Path, as_paused: bool = True) -> Task:
        data = json.loads(path.read_text(encoding="utf-8"))
        return await self.import_dict(data, as_paused=as_paused)
