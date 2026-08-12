"""The company: a roster of specialist personas that models are staffed into.

AETHER already separates two things — the **role** a piece of work needs
(architect, tester, reviewer) and the **provider** that executes it (a model or
a CLI). A roster adds the layer between them: the **persona**, a named
specialist with real domain expertise and a system prompt that makes a
general-purpose model behave like one.

    role      what kind of work this is        (9 values, drives routing)
    persona   who is doing it                  (hundreds, drives the prompt)
    provider  which model is behind them       (the adapter that runs)

So "Security Auditor, backed by GLM-4.5-Flash, doing a review subtask" is three
independent choices, and each can fail over without disturbing the others: if
GLM rate-limits, the Security Auditor keeps the job and moves to another model.
That is the whole reason personas are not providers.

The roster is read from disk and is entirely optional — AETHER works exactly as
before without one. Persona bodies are loaded lazily: a roster can hold several
hundred agents and parsing every system prompt at startup to use three of them
would be wasteful.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Where a roster usually lives, in order. Explicit configuration always wins;
# these are only so that the common case needs no configuration at all.
DEFAULT_ROSTER_LOCATIONS = (
    Path.home() / "Desktop" / "agentic ai",
    Path.home() / "agentic ai",
    Path.cwd() / "agentic ai",
)

ROSTER_INDEX = "digital-wizards-roster.json"
PERSONA_DIR = Path(".claude") / "agents"
DIVISIONS_FILE = Path("agency-agents") / "divisions.json"

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class Persona:
    """One specialist. Cheap to hold; the system prompt is fetched on demand."""

    slug: str
    name: str
    division: str
    description: str
    source: Path

    # Filled by load_prompt(); kept out of the index so 271 files are not read
    # to staff three of them.
    _prompt: Optional[str] = field(default=None, compare=False, repr=False)

    def load_prompt(self, max_chars: int = 6000) -> str:
        """The persona's system prompt, read from its definition file.

        Truncated because this is prepended to every request for this agent and
        competes with the task's own context for the model's window. Personas
        front-load identity and mission, so a head-truncation keeps the part
        that shapes behaviour and drops the long-tail examples.
        """
        if self._prompt is not None:
            return self._prompt
        try:
            raw = self.source.read_text(encoding="utf-8")
        except OSError:
            return ""
        body = _FRONTMATTER.sub("", raw, count=1).strip()
        if len(body) > max_chars:
            body = body[:max_chars].rsplit("\n", 1)[0] + "\n\n[persona truncated]"
        return body


@dataclass(frozen=True)
class Division:
    key: str
    label: str
    icon: str = ""
    color: str = ""


class Roster:
    """An in-memory index of the company.

    Deliberately dumb: it answers "who exists" and "who fits this". Deciding
    which model backs a persona is the router's job, and keeping those separate
    is what lets a persona survive its provider failing.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.personas: dict[str, Persona] = {}
        self.divisions: dict[str, Division] = {}
        self._load()

    # --- construction ----------------------------------------------------

    @classmethod
    def discover(cls, configured: Optional[str | Path] = None) -> Optional["Roster"]:
        """Find a roster, or return None. Absence is normal, not an error."""
        candidates: Iterable[Path]
        if configured:
            candidates = (Path(configured),)
        else:
            candidates = DEFAULT_ROSTER_LOCATIONS
        for path in candidates:
            if (path / ROSTER_INDEX).is_file():
                return cls(path)
        return None

    def _load(self) -> None:
        index = self.root / ROSTER_INDEX
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        entries = data if isinstance(data, list) else data.get("agents", [])
        persona_dir = self.root / PERSONA_DIR
        for entry in entries:
            slug = entry.get("slug")
            if not slug:
                continue
            self.personas[slug] = Persona(
                slug=slug,
                name=entry.get("name") or slug,
                division=entry.get("division") or "specialized",
                description=entry.get("description") or "",
                source=persona_dir / f"{slug}.md",
            )

        try:
            raw = json.loads((self.root / DIVISIONS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        items = raw if isinstance(raw, list) else raw.get("divisions", [])
        for item in items if isinstance(items, list) else []:
            key = item.get("key") or item.get("id") or item.get("slug")
            if key:
                self.divisions[key] = Division(
                    key=key,
                    label=item.get("label") or key.replace("-", " ").title(),
                    icon=item.get("icon", ""),
                    color=item.get("color", ""),
                )
        # A division present on personas but missing from the file still needs a
        # label, or the view would silently drop those agents.
        for persona in self.personas.values():
            self.divisions.setdefault(
                persona.division,
                Division(key=persona.division,
                         label=persona.division.replace("-", " ").title()),
            )

    # --- queries ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.personas)

    def get(self, slug: str) -> Optional[Persona]:
        return self.personas.get(slug)

    def by_division(self, division: str) -> list[Persona]:
        return sorted(
            (p for p in self.personas.values() if p.division == division),
            key=lambda p: p.name,
        )

    def division_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for persona in self.personas.values():
            counts[persona.division] = counts.get(persona.division, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def search(self, text: str, limit: int = 20) -> list[Persona]:
        """Find specialists matching any word in the query, best first.

        Word-based rather than substring: `who "payment security"` should find
        the security bench, not nothing because no single persona contains that
        exact phrase. Results are ranked by how many query words they hit, so a
        genuine multi-word match still floats to the top.
        """
        words = [w for w in text.lower().split() if len(w) > 2]
        if not words:
            return []

        scored: list[tuple[int, Persona]] = []
        for p in self.personas.values():
            haystack = f"{p.name} {p.slug} {p.division} {p.description}".lower()
            hits = sum(1 for w in words if w in haystack)
            if hits:
                # A name hit is worth more than a mention buried in prose.
                bonus = sum(1 for w in words if w in p.name.lower())
                scored.append((hits * 2 + bonus, p))

        scored.sort(key=lambda kv: (-kv[0], kv[1].division, kv[1].name))
        return [p for _, p in scored[:limit]]
