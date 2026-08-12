"""Deciding which specialist takes which subtask.

Deliberately a scored keyword match rather than an LLM call, for the same
reason the router is deterministic: staffing decisions should be reproducible
and explicable. "Why did the Security Auditor get this?" must have an answer
better than "the model felt like it", and a planning LLM that is down must not
be able to stop work from being assigned.

The scoring is intentionally simple and its weights are named. It will not
always pick the specialist a human would, which is exactly why the plan is
shown for approval before anything runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from aether.agentic.roster import Persona, Roster
from aether.domain.enums import AgentRole
from aether.domain.models import Subtask

# Which divisions plausibly serve which role. First match wins, so order
# matters: the earlier divisions are the more natural home for that role.
ROLE_DIVISIONS: dict[AgentRole, tuple[str, ...]] = {
    AgentRole.ARCHITECT: ("engineering", "product", "spatial-computing", "specialized"),
    AgentRole.IMPLEMENTER: ("engineering", "game-development", "gis", "specialized"),
    AgentRole.REVIEWER: ("engineering", "testing", "product"),
    AgentRole.SECURITY_REVIEWER: ("security", "engineering", "testing"),
    AgentRole.TESTER: ("testing", "engineering"),
    AgentRole.DEBUGGER: ("engineering", "testing"),
    AgentRole.RESEARCHER: ("academic", "specialized", "marketing", "product"),
    AgentRole.FINAL_REVIEWER: ("product", "project-management", "engineering"),
    AgentRole.GENERAL: (),
}

# Scoring weights. Named rather than inline so the ranking can be reasoned
# about, and so a change to it is visible in a diff.
W_TERM_IN_DESCRIPTION = 3.0
W_TERM_IN_NAME = 5.0          # a name match is a stronger signal than prose
W_DIVISION_FITS_ROLE = 6.0
W_DIVISION_RANK = 0.5         # earlier divisions in ROLE_DIVISIONS edge ahead

# The overall goal is context, not the assignment. Weighting it equally with
# the subtask made every subtask of "build a multiplayer game with payments and
# a launch page" match on "game": the marketing copy went to a Game Audio
# Engineer and the tests went to a Backend Architect. A goal term should break
# a tie between two plausible specialists, never choose the specialist.
W_GOAL_DISCOUNT = 0.2

# A qualifier in a persona's name that the request never mentions is evidence
# *against* them. Without this, one matching word beat everything: landing-page
# copy went to the "Healthcare Marketing Compliance Specialist" on "marketing",
# and checkout tests to the "Drupal Shopping Cart Engineer" on "checkout". Both
# are real specialists; neither was asked for. A narrow expert should win only
# when their narrowness was requested.
W_NAME_NOISE = -2.0

# Role nouns carry no specificity — every third persona is an "Engineer" — so
# they neither earn credit nor attract the penalty above.
GENERIC_NAME_WORDS = frozenset("""
engineer engineering developer specialist analyst architect designer manager
lead director officer consultant expert scientist strategist auditor reviewer
writer coordinator administrator advisor agent assistant master chief head
""".split())

_WORD = re.compile(r"[a-z][a-z0-9+#.-]{2,}")

# Words that appear in nearly every software goal and would otherwise match
# every engineer equally, adding noise without discriminating.
STOPWORDS = frozenset("""
the and for with that this from into your you our are was were will would
add build make create write update change fix using use used need needs
should must can could task work please help new all any get set run
""".split())


@dataclass(frozen=True)
class Assignment:
    """One staffing decision, with its reasoning attached."""

    subtask_id: str
    subtask_title: str
    role: AgentRole
    persona: Optional[Persona]
    score: float
    reasons: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        if self.persona is None:
            return "(unstaffed — no roster match)"
        return f"{self.persona.name} · {self.persona.division}"


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in STOPWORDS}


def score_persona(
    persona: Persona,
    terms: set[str],
    role: AgentRole,
    goal_terms: Optional[set[str]] = None,
) -> tuple[float, list[str]]:
    """How well this specialist fits this work, and why.

    `terms` come from the subtask and carry full weight. `goal_terms` come from
    the overall goal and are discounted — see W_GOAL_DISCOUNT.
    """
    score = 0.0
    reasons: list[str] = []

    name_terms = _terms(persona.name)
    desc_terms = _terms(persona.description)
    goal_terms = (goal_terms or set()) - terms  # never count a term twice

    hit_name = terms & name_terms
    if hit_name:
        score += W_TERM_IN_NAME * len(hit_name)
        reasons.append(f"name:{'/'.join(sorted(hit_name))}")

    hit_desc = terms & desc_terms
    if hit_desc:
        score += W_TERM_IN_DESCRIPTION * len(hit_desc)
        reasons.append(f"expertise:{'/'.join(sorted(list(hit_desc)[:3]))}")

    goal_hits = goal_terms & (name_terms | desc_terms)
    if goal_hits:
        score += W_TERM_IN_DESCRIPTION * W_GOAL_DISCOUNT * len(goal_hits)
        reasons.append(f"context:{'/'.join(sorted(list(goal_hits)[:2]))}")

    # Qualifiers in the name that nobody asked for.
    noise = (name_terms - GENERIC_NAME_WORDS) - terms - goal_terms
    if noise:
        score += W_NAME_NOISE * len(noise)
        reasons.append(f"unasked:{'/'.join(sorted(list(noise)[:2]))}")

    # The work itself can name a division the role mapping does not cover.
    # ROLE_DIVISIONS says an IMPLEMENTER lives in engineering, so "write the
    # marketing landing page copy" found no marketing home and went to whoever
    # merely had "marketing" in their name. Matching the division directly
    # lets the whole marketing bench compete, and the cleanest name wins.
    division_terms = _terms(persona.division.replace("-", " "))
    if terms & division_terms:
        score += W_DIVISION_FITS_ROLE
        reasons.append(f"division:{persona.division} named in the work")

    divisions = ROLE_DIVISIONS.get(role, ())
    if persona.division in divisions:
        rank = divisions.index(persona.division)
        score += W_DIVISION_FITS_ROLE + W_DIVISION_RANK * (len(divisions) - rank)
        reasons.append(f"division:{persona.division} fits {role.value}")

    return score, reasons


def staff_subtask(roster: Roster, subtask: Subtask, goal: str = "") -> Assignment:
    """Pick the best-fitting specialist for one subtask."""
    terms = _terms(f"{subtask.title} {subtask.description}")
    goal_terms = _terms(goal)
    best: Optional[Persona] = None
    best_score = 0.0
    best_reasons: list[str] = []

    for persona in roster.personas.values():
        score, reasons = score_persona(persona, terms, subtask.role, goal_terms)
        # Strict >: ties keep the first seen, so iteration order decides rather
        # than nothing. dict preserves insertion order, so this is stable
        # across runs on the same roster.
        if score > best_score:
            best, best_score, best_reasons = persona, score, reasons

    return Assignment(
        subtask_id=subtask.subtask_id,
        subtask_title=subtask.title,
        role=subtask.role,
        persona=best,
        score=best_score,
        reasons=tuple(best_reasons),
    )


def staff_task(roster: Roster, subtasks: list[Subtask], goal: str = "") -> list[Assignment]:
    """Staff a whole plan.

    A specialist may take more than one subtask — a company does not refuse to
    let its one security reviewer look at two things. Concurrency is the
    router's problem, and it already spreads load across providers.
    """
    return [staff_subtask(roster, s, goal) for s in subtasks]


def persona_preamble(persona: Persona, role: AgentRole) -> str:
    """The block prepended to a subtask prompt so the model plays the part.

    The role line matters as much as the persona: an Anthropologist assigned a
    review needs to know it is reviewing, not writing an ethnography.
    """
    prompt = persona.load_prompt()
    header = (
        f"You are **{persona.name}**, {persona.description}\n"
        f"You are acting as the **{role.value}** for this subtask.\n"
    )
    if not prompt:
        return header
    return f"{header}\n{prompt}\n\n---\n\n"
