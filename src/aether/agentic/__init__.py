"""Agentic view: staffing a company of specialist personas onto AETHER's models.

See `roster.py` for the persona/provider/role separation this is built on.
"""

from aether.agentic.roster import Division, Persona, Roster
from aether.agentic.staffing import Assignment, persona_preamble, staff_task

__all__ = [
    "Assignment",
    "Division",
    "Persona",
    "Roster",
    "persona_preamble",
    "staff_task",
]
