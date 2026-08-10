"""Plugin discovery for third-party adapters via entry points or import paths.

Registration paths:
1. Config: providers.<id>.adapter_class: "mypkg.adapters:MyAdapter"
2. Entry points group: aether.adapters
"""

from __future__ import annotations

import importlib
import sys
from typing import Any, Optional, Type

from aether.domain.models import ProviderConfig
from aether.observability.logging import get_logger

log = get_logger("aether.plugins")


def load_class(path: str) -> Type[Any]:
    """Load 'package.module:ClassName' or 'package.module.ClassName'."""
    if ":" in path:
        mod_name, cls_name = path.split(":", 1)
    else:
        parts = path.rsplit(".", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid adapter_class path: {path}")
        mod_name, cls_name = parts
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    return cls


def discover_entry_point_adapters() -> dict[str, Type[Any]]:
    found: dict[str, Type[Any]] = {}
    try:
        if sys.version_info >= (3, 10):
            from importlib.metadata import entry_points

            eps = entry_points()
            group = eps.select(group="aether.adapters") if hasattr(eps, "select") else eps.get("aether.adapters", [])
        else:
            from importlib.metadata import entry_points

            group = entry_points().get("aether.adapters", [])  # type: ignore[assignment]
        for ep in group:
            try:
                found[ep.name] = ep.load()
                log.info("plugin_loaded", name=ep.name, value=ep.value)
            except Exception as exc:  # noqa: BLE001
                log.warning("plugin_load_failed", name=ep.name, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("entry_point_scan_failed", error=str(exc))
    return found


def create_plugin_adapter(config: ProviderConfig, **kwargs: Any) -> Optional[Any]:
    """Create adapter from config.extra['adapter_class'] if present."""
    path = (config.extra or {}).get("adapter_class")
    if not path:
        # also allow top-level via extra passthrough from yaml unknown keys
        return None
    try:
        cls = load_class(str(path))
        return cls(config, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.error("plugin_adapter_create_failed", provider=config.provider_id, error=str(exc))
        return None
