"""Ship the built dashboard in the wheel — but only when it has been built.

`[tool.hatch.build.targets.wheel.force-include]` cannot express "if present".
Listing `web/dist` there unconditionally made `pip install -e .` fail outright
with `FileNotFoundError: Forced include not found: web/dist` on any checkout
where the dashboard had not been compiled — which is every CI job and every
fresh clone, since `web/dist` is a build artifact and gitignored.

A build hook can ask the filesystem, so it does:

  * dashboard built  -> the wheel carries it at `aether/resources/dashboard`
  * not built        -> the package installs cleanly, without one

The second case is the normal state for anyone working on the Python side, and
it has to keep working: needing Node installed to run `pytest` would be a
strange tax on a change to the orchestrator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

DASHBOARD_SRC = Path("web") / "dist"
DASHBOARD_DEST = "aether/resources/dashboard"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        dist = root / DASHBOARD_SRC
        if not dist.is_dir() or not any(dist.iterdir()):
            # Say so once. A wheel that silently lacks its dashboard is worse
            # than one that never claimed to have it.
            self.app.display_warning(
                "web/dist not built — packaging without the dashboard. "
                "Run `npm ci && npm run build` in web/ to include it."
            )
            return
        build_data.setdefault("force_include", {})[str(dist)] = DASHBOARD_DEST
