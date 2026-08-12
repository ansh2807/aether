"""Load an external skill library in the agentskills.io `SKILL.md` format.

Points at a directory of skills rather than vendoring one. The same choice as
the persona roster in `agentic/roster.py`, for the same reasons: a library of
several hundred skills belongs to whoever maintains it, a copy inside this
repository is a fork that silently goes stale, and the operator should be able
to see and change what their orchestrator is willing to do.

    aether skills --path ~/src/Anthropic-Cybersecurity-Skills

**Nothing is loaded by default.** AETHER ships exactly one skill of its own,
`skills/cyber`, which reviews the code its agents write. Everything else is
something the operator deliberately points it at, on their machine, under their
own authority.

## On offensive content

Real security libraries contain offensive material — red teaming, C2, evasion —
alongside the defensive majority. That is normal for the field and legitimate
under authorization: it is how people test their own systems.

This loader is content-neutral, but not silent. Every skill is classified from
its own declared metadata, `aether skills` shows the split, and offensive
skills are **never** applied automatically to a task. They are listed, and
running one is an explicit act by the person who loaded them. An orchestrator
that quietly acquired attack capability because a directory was on disk would
be indefensible; one that refuses to read a directory the operator chose would
be paternalistic and useless. Visible and opt-in is the line.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

SKILL_FILE = "SKILL.md"
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Classified from the skill's own declared domain and tags, not guessed from
# prose. A library that labels its own material is more reliable than a keyword
# search over it, and being wrong here has consequences in both directions.
OFFENSIVE_MARKERS = frozenset({
    "red-team", "red-teaming", "offensive", "exploitation", "exploit",
    "penetration-testing", "pentest", "c2", "command-and-control",
    "evasion", "post-exploitation", "lateral-movement", "privilege-escalation",
    "initial-access", "adversary-emulation",
})
DEFENSIVE_MARKERS = frozenset({
    "defensive", "blue-team", "threat-hunting", "detection", "forensics",
    "digital-forensics", "incident-response", "malware-analysis", "hardening",
    "monitoring", "threat-intelligence", "d3fend", "compliance", "audit",
})

OFFENSIVE, DEFENSIVE, DUAL_USE = "offensive", "defensive", "dual-use"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    domain: str
    tags: tuple[str, ...]
    posture: str
    source: Path
    frameworks: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def is_offensive(self) -> bool:
        return self.posture == OFFENSIVE

    def body(self, max_chars: int = 8000) -> str:
        """The instructions themselves, read on demand."""
        try:
            raw = self.source.read_text(encoding="utf-8")
        except OSError:
            return ""
        text = _FRONTMATTER.sub("", raw, count=1).strip()
        return text[:max_chars]


def _parse_frontmatter(raw: str) -> dict[str, object]:
    """Read the YAML header without requiring a YAML parse of untrusted input.

    Deliberately shallow: scalars and simple lists only. A skill library is
    third-party content, and `yaml.safe_load` over hundreds of files from a
    directory someone pointed us at is a wider door than this needs.
    """
    match = _FRONTMATTER.match(raw)
    if not match:
        return {}
    data: dict[str, object] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key or key != key.lstrip():
            continue  # nested key; not needed here
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("\"'") for v in value[1:-1].split(",")]
            data[key] = tuple(v for v in items if v)
        else:
            data[key] = value.strip("\"'")
    return data


def classify(domain: str, tags: Iterable[str]) -> str:
    """Offensive, defensive, or both — from what the skill says about itself."""
    vocab = {t.lower() for t in tags} | set(domain.lower().replace("/", "-").split())
    vocab |= {domain.lower()}
    offensive = bool(vocab & OFFENSIVE_MARKERS)
    defensive = bool(vocab & DEFENSIVE_MARKERS)
    if offensive and defensive:
        return DUAL_USE
    if offensive:
        return OFFENSIVE
    if defensive:
        return DEFENSIVE
    # Unmarked. Treated as dual-use rather than defensive: assuming the safer
    # reading of an unlabelled security skill is how offensive material ends up
    # applied automatically.
    return DUAL_USE


class SkillCatalog:
    """An index over an external skill library."""

    def __init__(self, root: Path | str, limit: int = 2000) -> None:
        self.root = Path(root)
        self.skills: dict[str, Skill] = {}
        self._load(limit)

    @classmethod
    def discover(cls, path: Optional[str | Path]) -> Optional["SkillCatalog"]:
        if not path:
            return None
        root = Path(path)
        if not root.is_dir():
            return None
        catalog = cls(root)
        return catalog if catalog.skills else None

    def _load(self, limit: int) -> None:
        # index.json is the library's own manifest and far cheaper than walking
        # a few thousand directories; fall back to a walk when absent.
        index = self.root / "index.json"
        if index.is_file():
            try:
                json.loads(index.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

        for count, path in enumerate(sorted(self.root.rglob(SKILL_FILE))):
            if count >= limit:
                break
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = _parse_frontmatter(raw)
            name = str(meta.get("name") or path.parent.name)
            if not name:
                continue
            tags = meta.get("tags")
            tags = tuple(tags) if isinstance(tags, tuple) else ()
            domain = str(meta.get("domain") or "")
            frameworks = {
                key: tuple(value)
                for key, value in meta.items()
                if isinstance(value, tuple)
                and key in {"atlas_techniques", "d3fend_techniques", "nist_csf",
                            "nist_ai_rmf", "cwe", "owasp_top_10", "attack_techniques"}
            }
            self.skills[name] = Skill(
                name=name,
                description=str(meta.get("description") or ""),
                domain=domain,
                tags=tags,
                posture=classify(domain, tags),
                source=path,
                frameworks=frameworks,
            )

    # --- queries ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.skills)

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)

    def posture_counts(self) -> dict[str, int]:
        counts = {DEFENSIVE: 0, DUAL_USE: 0, OFFENSIVE: 0}
        for skill in self.skills.values():
            counts[skill.posture] = counts.get(skill.posture, 0) + 1
        return counts

    def domains(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for skill in self.skills.values():
            key = skill.domain or "uncategorised"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    def search(self, query: str, limit: int = 25) -> list[Skill]:
        words = [w for w in query.lower().split() if len(w) > 1]
        if not words:
            return []
        scored: list[tuple[int, Skill]] = []
        for skill in self.skills.values():
            hay = f"{skill.name} {skill.domain} {skill.description} {' '.join(skill.tags)}".lower()
            hits = sum(1 for w in words if w in hay)
            if hits:
                scored.append((hits * 2 + sum(1 for w in words if w in skill.name.lower()), skill))
        scored.sort(key=lambda kv: (-kv[0], kv[1].name))
        return [s for _, s in scored[:limit]]

    def auto_applicable(self) -> list[Skill]:
        """Skills an orchestrator may apply to a task without being asked.

        Defensive only. Offensive and unlabelled skills are listed and
        searchable but never reached for on their own — acquiring attack
        capability because a directory happened to be on disk is not a
        behaviour anyone consented to.
        """
        return sorted(
            (s for s in self.skills.values() if s.posture == DEFENSIVE),
            key=lambda s: s.name,
        )
