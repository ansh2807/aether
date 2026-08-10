"""AgentAdapter protocol — provider-independent interface."""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from aether.domain.models import (
    AuthStatus,
    CapabilityProfile,
    ExecutionRequest,
    ExecutionResult,
    HealthStatus,
    LimitSnapshot,
    StreamChunk,
    UsageSnapshot,
)


@runtime_checkable
class AgentAdapter(Protocol):
    """Every provider implements this interface."""

    provider_id: str

    async def initialize(self) -> None: ...

    async def authenticate(self) -> AuthStatus: ...

    async def health_check(self) -> HealthStatus: ...

    async def get_capabilities(self) -> CapabilityProfile: ...

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...

    async def stream(self, request: ExecutionRequest) -> AsyncIterator[StreamChunk]: ...

    async def cancel(self, execution_id: str) -> None: ...

    async def cancel_all(self) -> None: ...

    async def get_usage(self) -> UsageSnapshot: ...

    async def get_limits(self) -> LimitSnapshot: ...

    async def shutdown(self) -> None: ...
