"""Execution sandbox and command policy."""

from __future__ import annotations

from pathlib import Path

from aether.domain.enums import ApprovalLevel
from aether.config.settings import SecuritySettings


class SecurityError(Exception):
    pass


class ApprovalRequired(SecurityError):
    def __init__(self, operation: str, detail: str = "") -> None:
        self.operation = operation
        super().__init__(f"Approval required for: {operation}. {detail}".strip())


class ExecutionSandbox:
    """Enforces workspace isolation and command policy."""

    DANGEROUS_OPS = (
        "delete",
        "rm ",
        "rmdir",
        "del ",
        "push",
        "deploy",
        "install",
        "pip install",
        "npm install",
        "npm i ",
        "drop ",
        "format",
    )

    def __init__(
        self,
        settings: SecuritySettings,
        approval_level: ApprovalLevel | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.settings = settings
        self.approval_level = approval_level or settings.approval_level
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self._approvals: set[str] = set()

    def grant(self, operation: str) -> None:
        self._approvals.add(operation)

    def resolve_path(self, workspace: Path, path: str | Path) -> Path:
        workspace = workspace.resolve()
        target = (workspace / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if self.settings.workspace_only:
            try:
                target.relative_to(workspace)
            except ValueError as exc:
                raise SecurityError(f"Path escapes workspace: {target}") from exc
        return target

    def check_workspace(self, path: str | Path) -> Path:
        """Ensure a subprocess cwd stays inside the configured workspace root."""
        target = Path(path).resolve()
        if not self.settings.workspace_only or self.workspace_root is None:
            return target
        try:
            target.relative_to(self.workspace_root)
        except ValueError as exc:
            raise SecurityError(
                f"Execution directory escapes workspace root {self.workspace_root}: {target}"
            ) from exc
        return target

    def check_command(self, command: str, auto_approve: bool = False) -> None:
        lower = command.strip().lower()
        if not lower:
            return
        for denied in self.settings.command_denylist:
            if denied.lower() in lower:
                raise SecurityError(f"Command denied by policy: matches '{denied}'")

        if self.approval_level == ApprovalLevel.AUTO:
            return

        if self.approval_level == ApprovalLevel.SAFE:
            # block clearly destructive; require approval for install/push/deploy
            for op in self.DANGEROUS_OPS:
                if op in lower:
                    if auto_approve or op in self._approvals or command in self._approvals:
                        return
                    raise ApprovalRequired(op, f"Command: {command[:200]}")
            # if allowlist configured, require prefix match for non-empty allowlist
            prefixes = self.settings.command_allowlist_prefixes
            if prefixes:
                if not any(lower.startswith(p.strip().lower()) for p in prefixes):
                    # allow if it's not shell-exec intensive — still require soft gate
                    # NOTE: `lower` is already lower-cased — patterns must be too.
                    if any(x in lower for x in ("curl ", "wget ", "invoke-webrequest", "powershell ")):
                        raise ApprovalRequired("network_command", command[:200])
            return

        # APPROVAL: everything needs explicit grant except read-only-ish prefixes
        readonly = ("ls ", "dir ", "cat ", "type ", "pwd", "git status", "git log", "git diff")
        if any(lower.startswith(r) or lower == r.strip() for r in readonly):
            return
        if auto_approve or command in self._approvals:
            return
        raise ApprovalRequired("command", command[:200])

    def require_approval(self, operation: str, detail: str = "") -> None:
        if self.approval_level == ApprovalLevel.AUTO:
            return
        if operation in self._approvals:
            return
        if self.approval_level == ApprovalLevel.SAFE and operation not in {
            "delete_files", "install_packages", "git_push", "deploy", "destructive_shell"
        }:
            return
        raise ApprovalRequired(operation, detail)
