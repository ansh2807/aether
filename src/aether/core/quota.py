"""Provider usage / quota manager."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from aether.config.settings import AetherSettings
from aether.core.state_store import JsonStateStore
from aether.domain.enums import ProviderAvailability
from aether.domain.models import LimitSnapshot, UsageSnapshot, utcnow
from aether.storage.repository import SqliteRepository


def _today() -> str:
    return utcnow().strftime("%Y-%m-%d")


class QuotaManager:
    """Per-provider request accounting.

    Daily counters roll over at UTC midnight, and usage is persisted so a
    restart does not forget quota already spent against a provider.
    """

    def __init__(
        self,
        settings: AetherSettings,
        repo: Optional[SqliteRepository] = None,
        store: Optional[JsonStateStore] = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.store = store
        self._usage: dict[str, UsageSnapshot] = {}
        self._cooldowns: dict[str, datetime] = {}
        self._window: dict[str, list[datetime]] = {}
        self._reload()

    # --- persistence -----------------------------------------------------

    def _transaction(self):
        """Exclusive lock across reload+mutate+flush; no-op without a store."""
        if self.store is None:
            from contextlib import nullcontext

            return nullcontext()
        return self.store.transaction()

    def _reload(self) -> None:
        if self.store is None:
            return
        data = self.store.load()
        try:
            self._usage = {
                pid: UsageSnapshot.model_validate(item)
                for pid, item in (data.get("usage") or {}).items()
            }
            self._cooldowns = {
                pid: datetime.fromisoformat(value)
                for pid, value in (data.get("cooldowns") or {}).items()
            }
        except Exception:  # noqa: BLE001 - corrupt state must not break the run
            pass

    def _flush(self) -> None:
        if self.store is None:
            return
        self.store.save(
            {
                "usage": {pid: u.model_dump(mode="json") for pid, u in self._usage.items()},
                "cooldowns": {pid: dt.isoformat() for pid, dt in self._cooldowns.items()},
            }
        )

    def _get(self, provider_id: str) -> UsageSnapshot:
        usage = self._usage.get(provider_id)
        if usage is None:
            usage = UsageSnapshot(provider_id=provider_id, day=_today())
            self._usage[provider_id] = usage
        today = _today()
        if usage.day != today:
            # new UTC day — the daily allowance resets
            usage.daily_requests = 0
            usage.day = today
        return usage

    def _limits_for(self, provider_id: str) -> LimitSnapshot:
        cfg = self.settings.providers.get(provider_id)
        limits = (cfg.limits if cfg else {}) or {}
        return LimitSnapshot(
            requests_per_minute=limits.get(
                "requests_per_minute", self.settings.quota.default_requests_per_minute
            ),
            daily_requests=limits.get("daily_requests", self.settings.quota.default_daily_requests),
            cooldown_until=self._cooldowns.get(provider_id),
        )

    def availability(self, provider_id: str) -> ProviderAvailability:
        now = utcnow()
        cd = self._cooldowns.get(provider_id)
        if cd and now < cd:
            return ProviderAvailability.RATE_LIMITED
        usage = self._get(provider_id)
        limits = self._limits_for(provider_id)
        if limits.daily_requests is not None and usage.daily_requests >= limits.daily_requests:
            return ProviderAvailability.QUOTA_EXHAUSTED
        # RPM window
        window = self._window.setdefault(provider_id, [])
        cutoff = now - timedelta(minutes=1)
        self._window[provider_id] = [t for t in window if t > cutoff]
        rpm = limits.requests_per_minute or self.settings.quota.default_requests_per_minute
        if len(self._window[provider_id]) >= rpm:
            return ProviderAvailability.RATE_LIMITED
        return ProviderAvailability.AVAILABLE

    def record_request(self, provider_id: str, tokens: int = 0) -> UsageSnapshot:
        with self._transaction():
            self._reload()
            usage = self._get(provider_id)
            usage.requests += 1
            usage.daily_requests += 1
            usage.estimated_tokens += tokens
            usage.last_request_at = utcnow()
            self._window.setdefault(provider_id, []).append(utcnow())
            self._flush()
        return usage

    def record_failure(self, provider_id: str) -> UsageSnapshot:
        with self._transaction():
            self._reload()
            usage = self._get(provider_id)
            usage.failures += 1
            self._flush()
        return usage

    def set_cooldown(self, provider_id: str, seconds: int) -> None:
        with self._transaction():
            self._reload()
            self._cooldowns[provider_id] = utcnow() + timedelta(seconds=seconds)
            self._flush()

    def mark_quota_exhausted(self, provider_id: str) -> None:
        with self._transaction():
            self._reload()
            usage = self._get(provider_id)
            limits = self._limits_for(provider_id)
            if limits.daily_requests:
                usage.daily_requests = limits.daily_requests
            self._flush()

    def get_usage(self, provider_id: str) -> UsageSnapshot:
        return self._get(provider_id)

    def get_limits(self, provider_id: str) -> LimitSnapshot:
        usage = self._get(provider_id)
        limits = self._limits_for(provider_id)
        if limits.daily_requests is not None:
            limits.remaining_daily = max(0, limits.daily_requests - usage.daily_requests)
        return limits
