"""Agent registry — construct and hold adapters from configuration."""

from __future__ import annotations

from typing import Any, Optional

from aether.adapters.api_adapter import ApiAgentAdapter
from aether.adapters.cli_adapter import CliAgentAdapter
from aether.adapters.mock_adapter import MockAdapter
from aether.adapters.opencode_adapter import OpencodeCliAdapter
from aether.adapters.plugins import create_plugin_adapter, discover_entry_point_adapters
from aether.adapters.unavailable import UnavailableAdapter
from aether.config.settings import AetherSettings
from aether.domain.enums import AdapterType, ProviderAvailability
from aether.domain.models import CapabilityProfile, HealthStatus, ProviderConfig
from aether.interfaces.adapters import AgentAdapter
from aether.observability.logging import get_logger
from aether.security.sandbox import ExecutionSandbox

log = get_logger("aether.registry")


class AgentRegistry:
    def __init__(
        self,
        settings: AetherSettings,
        sandbox: Optional[ExecutionSandbox] = None,
        include_test_adapters: bool = True,
    ) -> None:
        self.settings = settings
        self.sandbox = sandbox
        self.include_test_adapters = include_test_adapters
        self._adapters: dict[str, AgentAdapter] = {}
        self._configs: dict[str, ProviderConfig] = dict(settings.providers)
        self._entry_points: dict[str, Any] = {}

    def register(self, adapter: AgentAdapter, config: Optional[ProviderConfig] = None) -> None:
        self._adapters[adapter.provider_id] = adapter
        if config:
            self._configs[adapter.provider_id] = config
        log.info("provider_registered", provider=adapter.provider_id)

    def build_from_config(self) -> None:
        self._entry_points = discover_entry_point_adapters()
        for pid, cfg in self._configs.items():
            if cfg.adapter == AdapterType.MOCK and not (self.include_test_adapters or cfg.enabled):
                continue
            adapter = self._create_adapter(cfg)
            if adapter is not None:
                self._adapters[pid] = adapter

    def _create_adapter(self, cfg: ProviderConfig) -> Optional[AgentAdapter]:
        # Explicit plugin class path wins
        plugin = create_plugin_adapter(cfg, sandbox=self.sandbox)
        if plugin is not None:
            return plugin
        # Entry point named after provider_id
        if cfg.provider_id in self._entry_points:
            try:
                return self._entry_points[cfg.provider_id](cfg, sandbox=self.sandbox)
            except TypeError:
                return self._entry_points[cfg.provider_id](cfg)
            except Exception as exc:  # noqa: BLE001
                log.warning("entry_point_adapter_failed", provider=cfg.provider_id, error=str(exc))

        # Open Code multi-model CLI
        if cfg.provider_id == "opencode_cli" or cfg.provider_id.startswith("oc_"):
            return OpencodeCliAdapter(
                cfg,
                sandbox=self.sandbox,
                max_output_bytes=self.settings.security.max_subprocess_output_bytes,
            )
        if (cfg.extra or {}).get("via") == "opencode" or (
            cfg.command == "opencode" and cfg.adapter == AdapterType.CLI
        ):
            return OpencodeCliAdapter(
                cfg,
                sandbox=self.sandbox,
                max_output_bytes=self.settings.security.max_subprocess_output_bytes,
            )

        if cfg.adapter == AdapterType.MOCK:
            return MockAdapter(cfg)
        if cfg.adapter == AdapterType.CLI:
            return CliAgentAdapter(
                cfg,
                sandbox=self.sandbox,
                max_output_bytes=self.settings.security.max_subprocess_output_bytes,
            )
        if cfg.adapter == AdapterType.API:
            return ApiAgentAdapter(cfg)
        if cfg.adapter == AdapterType.UNAVAILABLE:
            return UnavailableAdapter(cfg)
        return UnavailableAdapter(cfg)

    async def initialize_all(self) -> None:
        self.build_from_config()
        for adapter in self._adapters.values():
            try:
                await adapter.initialize()
            except Exception as exc:  # noqa: BLE001
                log.warning("provider_init_failed", provider=adapter.provider_id, error=str(exc))

    async def shutdown_all(self) -> None:
        for adapter in self._adapters.values():
            try:
                await adapter.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def get(self, provider_id: str) -> Optional[AgentAdapter]:
        return self._adapters.get(provider_id)

    def list_ids(self) -> list[str]:
        return sorted(self._adapters.keys())

    def get_config(self, provider_id: str) -> Optional[ProviderConfig]:
        return self._configs.get(provider_id)

    def list_configs(self) -> list[ProviderConfig]:
        return list(self._configs.values())

    async def health_all(self) -> list[HealthStatus]:
        results: list[HealthStatus] = []
        for pid, adapter in self._adapters.items():
            cfg = self._configs.get(pid)
            if cfg and not cfg.enabled and cfg.adapter != AdapterType.MOCK:
                results.append(
                    HealthStatus(
                        provider_id=pid,
                        available=False,
                        availability=ProviderAvailability.DISABLED,
                        message="disabled",
                    )
                )
                continue
            try:
                results.append(await adapter.health_check())
            except Exception as exc:  # noqa: BLE001
                results.append(
                    HealthStatus(
                        provider_id=pid,
                        available=False,
                        availability=ProviderAvailability.UNAVAILABLE,
                        message=str(exc),
                    )
                )
        return results

    async def capabilities(self, provider_id: str) -> CapabilityProfile:
        adapter = self.get(provider_id)
        if adapter is None:
            return CapabilityProfile()
        return await adapter.get_capabilities()

    def enabled_providers(self) -> list[str]:
        out = []
        for pid, cfg in self._configs.items():
            if pid not in self._adapters:
                continue
            if cfg.enabled or cfg.adapter == AdapterType.MOCK:
                out.append(pid)
        return out
