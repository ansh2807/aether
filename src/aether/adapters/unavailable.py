"""Stub adapter for providers that require future integration."""

from __future__ import annotations

from aether.adapters.base import BaseAdapter
from aether.domain.enums import FailureClass, ProviderAvailability
from aether.domain.models import (
    AuthStatus,
    ExecutionRequest,
    ExecutionResult,
    HealthStatus,
    ProviderConfig,
    utcnow,
)


class UnavailableAdapter(BaseAdapter):
    """Marks provider as ADAPTER_REQUIRED / UNAVAILABLE. Does not fake success."""

    async def authenticate(self) -> AuthStatus:
        return AuthStatus(
            authenticated=False,
            method="none",
            message=self.config.status_note or "ADAPTER_REQUIRED",
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            provider_id=self.provider_id,
            available=False,
            availability=ProviderAvailability.ADAPTER_REQUIRED,
            message=self.config.status_note or "UNAVAILABLE / ADAPTER_REQUIRED",
            checked_at=utcnow(),
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        msg = self.config.status_note or f"Provider {self.provider_id} has no adapter implementation"
        return ExecutionResult(
            execution_id=request.execution_id,
            provider_id=self.provider_id,
            success=False,
            error=msg,
            failure_class=FailureClass.UNKNOWN,
        )
