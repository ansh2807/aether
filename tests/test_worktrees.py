"""WO-17: each agent gets its own checkout.

`checkout_agent_branch` switched the one shared working tree, so two agents
could not hold different branches at once — whichever ran second overwrote the
first's files on disk. A worktree per agent makes concurrent editing real,
while merges still go through the normal path and still surface conflicts.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from aether.core.workspace import WorkspaceManager

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


async def _repo(tmp_path: Path) -> tuple[WorkspaceManager, Path]:
    mgr = WorkspaceManager(enable_git=True)
    ws = tmp_path / "workspace" / "TASK-1"
    ws.mkdir(parents=True)
    await mgr.ensure_repo(ws)
    (ws / "shared.txt").write_text("base\n", encoding="utf-8")
    await mgr._run(ws, "add", "-A")
    await mgr._run(ws, "commit", "-m", "base")
    return mgr, ws


@needs_git
@pytest.mark.asyncio
async def test_each_agent_gets_its_own_checkout(tmp_path: Path):
    mgr, ws = await _repo(tmp_path)

    branch_a, dir_a = await mgr.create_agent_worktree(ws, "agent_a", "sub1")
    branch_b, dir_b = await mgr.create_agent_worktree(ws, "agent_b", "sub2")

    assert dir_a != dir_b, "both agents were handed the same directory"
    assert dir_a != ws and dir_b != ws, "agents are still using the primary tree"
    assert (dir_a / "shared.txt").exists(), "the worktree did not get the repo content"
    assert (dir_b / "shared.txt").exists()
    # inside workspace_root, so the execution sandbox will accept them as cwd
    assert tmp_path / "workspace" in dir_a.parents


@needs_git
@pytest.mark.asyncio
async def test_agents_edit_the_same_file_simultaneously(tmp_path: Path):
    """The thing that was impossible before: no overwriting, no lock."""
    mgr, ws = await _repo(tmp_path)

    branch_a, dir_a = await mgr.create_agent_worktree(ws, "agent_a", "sub1")
    branch_b, dir_b = await mgr.create_agent_worktree(ws, "agent_b", "sub2")

    async def edit(directory: Path, text: str) -> None:
        await asyncio.sleep(0)  # interleave them
        (directory / "shared.txt").write_text(text, encoding="utf-8")

    await asyncio.gather(edit(dir_a, "A\n"), edit(dir_b, "B\n"))

    # neither agent clobbered the other, because they are different files
    assert (dir_a / "shared.txt").read_text(encoding="utf-8") == "A\n"
    assert (dir_b / "shared.txt").read_text(encoding="utf-8") == "B\n"
    assert (ws / "shared.txt").read_text(encoding="utf-8") == "base\n", (
        "an agent wrote into the primary tree"
    )


@needs_git
@pytest.mark.asyncio
async def test_worktree_work_merges_and_the_checkout_is_removed(tmp_path: Path):
    mgr, ws = await _repo(tmp_path)

    branch, work = await mgr.create_agent_worktree(ws, "agent_a", "sub1")
    (work / "feature.txt").write_text("done\n", encoding="utf-8")
    await mgr.commit_agent_work(work, "agent_a", "adds feature")

    ok, conflicts, _ = await mgr.merge_to_main(ws, branch)
    assert ok and not conflicts
    assert (ws / "feature.txt").exists(), "merged work did not reach the primary tree"

    await mgr.remove_agent_worktree(ws, branch)
    assert not work.exists(), "the checkout was left behind after merging"


@needs_git
@pytest.mark.asyncio
async def test_conflicts_still_surface_with_worktrees(tmp_path: Path):
    """Isolation must not cost us conflict detection."""
    mgr, ws = await _repo(tmp_path)

    branch_a, dir_a = await mgr.create_agent_worktree(ws, "agent_a", "sub1")
    branch_b, dir_b = await mgr.create_agent_worktree(ws, "agent_b", "sub2")

    (dir_a / "shared.txt").write_text("A\n", encoding="utf-8")
    await mgr.commit_agent_work(dir_a, "agent_a", "A work")
    (dir_b / "shared.txt").write_text("B\n", encoding="utf-8")
    await mgr.commit_agent_work(dir_b, "agent_b", "B work")

    ok_a, _, _ = await mgr.merge_to_main(ws, branch_a)
    assert ok_a

    ok_b, conflicts, _ = await mgr.merge_to_main(ws, branch_b)
    assert not ok_b, "the second agent's conflicting change was silently accepted"
    assert "shared.txt" in conflicts
    assert (ws / "shared.txt").read_text(encoding="utf-8").strip() == "A"


@needs_git
@pytest.mark.asyncio
async def test_falls_back_to_the_shared_tree_when_worktrees_fail(tmp_path: Path):
    """Worktrees are an optimisation; losing them must not fail the subtask."""
    mgr, ws = await _repo(tmp_path)

    async def broken(workspace, *args):
        if args and args[0] == "worktree":
            return 1, "simulated failure"
        return await WorkspaceManager._run(mgr, workspace, *args)

    mgr._run = broken  # type: ignore[method-assign]
    branch, work = await mgr.create_agent_worktree(ws, "agent_a", "sub1")

    assert work == ws, "should have fallen back to the shared working tree"
    assert branch


@pytest.mark.asyncio
async def test_no_git_means_no_worktree_and_no_error(tmp_path: Path):
    mgr = WorkspaceManager(enable_git=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    branch, work = await mgr.create_agent_worktree(ws, "agent_a", "sub1")
    assert work == ws
    await mgr.remove_agent_worktree(ws, branch)  # must not raise
