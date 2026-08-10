"""Domain model and handoff parsing tests."""

from aether.adapters.base import BaseAdapter
from aether.domain.enums import HandoffStatus, TaskStatus, TASK_TRANSITIONS
from aether.domain.models import ContextPackage, new_task_id


def test_task_id_format():
    tid = new_task_id()
    assert tid.startswith("TASK-")
    assert len(tid) > 10


def test_task_transitions_complete_terminal():
    assert TaskStatus.COMPLETED in TASK_TRANSITIONS
    assert TASK_TRANSITIONS[TaskStatus.COMPLETED] == set()


def test_parse_handoff_structured():
    text = """
STATUS: PARTIAL

COMPLETED:
- Auth module
- DB schema

REMAINING:
- OAuth callback
- token refresh

FILES:
- auth/
- database/

TESTS:
- 17 passed
- 2 failed

KNOWN_BUGS:
- flaky test

ASSUMPTIONS:
- Postgres available

RECOMMENDED_NEXT_ACTION:
Implement OAuth callback
"""
    ho = BaseAdapter.parse_handoff("TASK-1", "claude", text, requested="build auth")
    assert ho.status == HandoffStatus.PARTIAL
    assert "Auth module" in ho.completed
    assert "OAuth callback" in ho.remaining
    assert "auth/" in ho.files_changed
    assert "Implement OAuth callback" in ho.recommended_next_action


def test_parse_handoff_malformed():
    ho = BaseAdapter.parse_handoff("TASK-1", "x", "just some free text without structure")
    assert "malformed_handoff" in ho.known_bugs
    assert ho.status == HandoffStatus.PARTIAL


def test_context_package_prompt():
    pkg = ContextPackage(
        task_id="TASK-1",
        goal="Build app",
        current_state="halfway",
        your_assignment="fix tests",
        decisions=["use React"],
        known_issues=["flaky ICE"],
    )
    block = pkg.to_prompt_block()
    assert "Build app" in block
    assert "fix tests" in block
    assert "HANDOFF REPORT" in block
