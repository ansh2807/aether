"""Core enumerations for AETHER."""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    HANDOFF = "HANDOFF"
    REVIEW = "REVIEW"
    TESTING = "TESTING"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SubtaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class TaskMode(str, Enum):
    SOLO = "solo"
    COLLAB = "collab"
    DEBATE = "debate"
    PARALLEL = "parallel"


class ApprovalLevel(str, Enum):
    AUTO = "auto"
    SAFE = "safe"
    APPROVAL = "approval"


class HandoffStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class MessageType(str, Enum):
    TASK = "TASK"
    PLAN = "PLAN"
    QUESTION = "QUESTION"
    PROPOSAL = "PROPOSAL"
    CRITIQUE = "CRITIQUE"
    ARTIFACT = "ARTIFACT"
    HANDOFF = "HANDOFF"
    ERROR = "ERROR"
    TEST_RESULT = "TEST_RESULT"
    DECISION = "DECISION"
    REQUEST_REVIEW = "REQUEST_REVIEW"
    COMPLETION = "COMPLETION"
    SYSTEM = "SYSTEM"


class VerificationStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class ProviderAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    UNAVAILABLE = "UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    DISABLED = "DISABLED"
    ADAPTER_REQUIRED = "ADAPTER_REQUIRED"
    BUSY = "BUSY"


class AdapterType(str, Enum):
    CLI = "cli"
    API = "api"
    MOCK = "mock"
    UNAVAILABLE = "unavailable"


class FailureClass(str, Enum):
    TIMEOUT = "TIMEOUT"
    CRASH = "CRASH"
    AUTH = "AUTH"
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA = "QUOTA"
    NETWORK = "NETWORK"
    CONTEXT = "CONTEXT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    MALFORMED_HANDOFF = "MALFORMED_HANDOFF"
    VERIFICATION = "VERIFICATION"
    POLICY_DENIED = "POLICY_DENIED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class AgentRole(str, Enum):
    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    SECURITY_REVIEWER = "security_reviewer"
    TESTER = "tester"
    DEBUGGER = "debugger"
    RESEARCHER = "researcher"
    FINAL_REVIEWER = "final_reviewer"
    GENERAL = "general"


# Legal task status transitions (from -> allowed to)
TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {
        TaskStatus.PLANNING,
        TaskStatus.WAITING,
        TaskStatus.CANCELLED,
        TaskStatus.PAUSED,
    },
    TaskStatus.PLANNING: {
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.WAITING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PAUSED,
    },
    TaskStatus.QUEUED: {
        TaskStatus.RUNNING,
        TaskStatus.WAITING,
        TaskStatus.CANCELLED,
        TaskStatus.PAUSED,
        TaskStatus.FAILED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.HANDOFF,
        TaskStatus.WAITING,
        TaskStatus.REVIEW,
        TaskStatus.TESTING,
        TaskStatus.PAUSED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.COMPLETED,
    },
    TaskStatus.WAITING: {
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.PLANNING,
        TaskStatus.PAUSED,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    },
    TaskStatus.HANDOFF: {
        TaskStatus.RUNNING,
        TaskStatus.REVIEW,
        TaskStatus.TESTING,
        TaskStatus.QUEUED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PAUSED,
    },
    TaskStatus.REVIEW: {
        TaskStatus.RUNNING,
        TaskStatus.TESTING,
        TaskStatus.HANDOFF,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PAUSED,
    },
    TaskStatus.TESTING: {
        TaskStatus.RUNNING,
        TaskStatus.REVIEW,
        TaskStatus.HANDOFF,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PAUSED,
    },
    TaskStatus.PAUSED: {
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    },
    TaskStatus.FAILED: {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.CANCELLED: set(),
}
