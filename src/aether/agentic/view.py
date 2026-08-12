"""Rendering the company: the roster, a staffing plan, and who is working now.

Three views, because they answer three different questions:

    org      who exists, by division            — "what can this company do?"
    plan     who would take which subtask       — "do I approve this?"
    board    who is working right now, on what  — "what is happening?"

The plan view is the one that matters. Staffing is a deterministic keyword
match: reliably right about the *division*, only approximate about the
individual. Showing the reasoning and requiring approval is what makes that
acceptable — an operator who disagrees can see why and override.
"""

from __future__ import annotations

from typing import Iterable, Optional

from rich.panel import Panel

from aether.agentic.roster import Roster
from aether.agentic.staffing import Assignment
from aether.cli.ui import DASH, console, data_table, truncate


def render_org(roster: Roster, division: Optional[str] = None) -> None:
    """The company as an org chart: divisions and their headcount."""
    if division:
        people = roster.by_division(division)
        if not people:
            console.print(f"[muted]No division '{division}'. Try:[/] "
                          f"{', '.join(sorted(roster.divisions))}")
            return
        table = data_table("Agent", "Expertise",
                           title=f"{roster.divisions[division].label} {DASH} {len(people)}")
        for p in people:
            table.add_row(f"[value]{p.name}[/]", f"[muted]{truncate(p.description, 78)}[/]")
        console.print(table)
        return

    counts = roster.division_counts()
    table = data_table(
        "Division",
        {"header": "Agents", "justify": "right"},
        "Example specialists",
        title=f"The company {DASH} {len(roster)} agents across {len(counts)} divisions",
    )
    for key, count in counts.items():
        label = roster.divisions[key].label
        sample = ", ".join(p.name for p in roster.by_division(key)[:3])
        table.add_row(f"[value]{label}[/]", str(count), f"[muted]{truncate(sample, 60)}[/]")
    console.print(table)
    console.print("\n[muted]One division:[/] [accent]aether agentic org --division engineering[/]")
    console.print("[muted]Find someone:[/] [accent]aether agentic who \"payment security\"[/]")


def render_plan(
    assignments: Iterable[Assignment],
    goal: str,
    providers: Optional[dict[str, str]] = None,
) -> None:
    """The staffing plan awaiting approval.

    Shows the reasoning per row on purpose. "Why did the Accessibility Auditor
    get the checkout tests?" should be answerable before the work runs, not
    after — that is the difference between a plan and a fait accompli.
    """
    providers = providers or {}
    console.print(Panel(f"[heading]{truncate(goal, 96)}[/]",
                        title="Proposed staffing", border_style="accent", padding=(0, 2)))

    table = data_table("#", "Subtask", "Role", "Assigned to", "Model", "Why")
    for i, a in enumerate(assignments, 1):
        who = f"[value]{a.persona.name}[/]" if a.persona else "[warn]unstaffed[/]"
        div = f"[muted]{a.persona.division}[/]" if a.persona else ""
        table.add_row(
            str(i),
            truncate(a.subtask_title, 34),
            a.role.value,
            f"{who}\n{div}" if div else who,
            providers.get(a.subtask_id, "[muted]auto[/]"),
            f"[muted]{truncate(', '.join(a.reasons) or 'no signal', 34)}[/]",
        )
    console.print(table)

    unstaffed = [a for a in assignments if a.persona is None]
    if unstaffed:
        console.print(
            f"[warn]{len(unstaffed)} subtask(s) matched nobody[/] [muted]{DASH} they will run "
            f"with no persona, exactly as AETHER behaves without a roster.[/]"
        )
    console.print(
        "[muted]Staffing is a keyword match: dependable about the division, "
        "approximate about\nthe individual. Override any row before approving.[/]"
    )


def render_board(
    roster: Roster,
    assignments: Iterable[Assignment],
    statuses: dict[str, str],
    providers: Optional[dict[str, str]] = None,
) -> None:
    """Who is working right now — the live floor of the company."""
    providers = providers or {}
    table = data_table("Agent", "Division", "Working on", "Model", "Status",
                       title="Agents at work")
    rows = 0
    for a in assignments:
        status = statuses.get(a.subtask_id, "PENDING")
        name = a.persona.name if a.persona else "(unstaffed)"
        table.add_row(
            f"[value]{name}[/]",
            f"[muted]{a.persona.division if a.persona else '-'}[/]",
            truncate(a.subtask_title, 36),
            providers.get(a.subtask_id, "-"),
            status,
        )
        rows += 1
    if not rows:
        console.print("[muted]Nobody is staffed yet.[/]")
        return
    console.print(table)
