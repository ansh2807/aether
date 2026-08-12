"""Loading an external skill library, and what the orchestrator will do with it.

The ask was "take everything from the cybersecurity skills repo". Vendoring 817
files would fork someone else's Apache-2.0 project into this one and freeze it,
so AETHER loads a library the operator points at instead — the same choice as
the persona roster.

Real security libraries contain offensive material next to a defensive
majority. That is normal and legitimate under authorization. The line drawn
here is not *whether the operator may load it* — their machine, their
authority — but whether the orchestrator may reach for it unprompted. It may
not, and these tests are what stop that from quietly changing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aether.skills.catalog import (
    DEFENSIVE,
    DUAL_USE,
    OFFENSIVE,
    SkillCatalog,
    classify,
)


def _skill(root: Path, name: str, domain: str, tags: str, extra: str = "") -> None:
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: performs {name}\n"
        f"domain: {domain}\ntags: {tags}\n{extra}---\n\n"
        f"# {name}\n\n## Workflow\n1. do the thing\n",
        encoding="utf-8",
    )


@pytest.fixture
def library(tmp_path: Path) -> SkillCatalog:
    _skill(tmp_path, "threat-hunt-evtx", "threat-hunting", "[threat-hunting, detection]")
    _skill(tmp_path, "malware-triage", "malware-analysis", "[malware-analysis, forensics]")
    _skill(tmp_path, "ir-containment", "incident-response", "[incident-response, detection]")
    _skill(tmp_path, "bloodhound-recon", "red-teaming", "[red-team, lateral-movement]")
    _skill(tmp_path, "sliver-c2", "red-teaming", "[c2, offensive, evasion]")
    _skill(tmp_path, "purple-team-atk", "adversary-emulation", "[red-team, detection]")
    _skill(tmp_path, "unlabelled", "", "[]")
    return SkillCatalog(tmp_path)


# --- loading -------------------------------------------------------------


def test_a_library_loads_from_a_directory(library: SkillCatalog):
    assert len(library) == 7
    assert library.get("sliver-c2") is not None


def test_no_library_means_nothing_loaded():
    """AETHER ships one skill of its own; everything else is opt-in."""
    assert SkillCatalog.discover(None) is None
    assert SkillCatalog.discover("/nope/does/not/exist") is None


def test_a_directory_with_no_skills_is_not_a_library(tmp_path: Path):
    (tmp_path / "readme.txt").write_text("nothing here", encoding="utf-8")
    assert SkillCatalog.discover(tmp_path) is None


def test_frontmatter_is_parsed_without_a_yaml_load(tmp_path: Path):
    """Third-party content from a directory someone pointed us at. A shallow
    reader is a narrower door than a YAML parser over hundreds of files."""
    _skill(tmp_path, "s", "detection", "[detection, siem]", extra="cwe: [CWE-79]\n")
    skill = SkillCatalog(tmp_path).get("s")
    assert skill.tags == ("detection", "siem")
    assert skill.frameworks["cwe"] == ("CWE-79",)


def test_a_malformed_skill_does_not_break_the_library(tmp_path: Path):
    _skill(tmp_path, "good", "detection", "[detection]")
    bad = tmp_path / "skills" / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("no frontmatter at all\n", encoding="utf-8")
    catalog = SkillCatalog(tmp_path)
    assert catalog.get("good") is not None
    assert len(catalog) == 2, "a headerless skill should fall back to its directory name"


def test_instructions_are_read_on_demand(library: SkillCatalog):
    """Hundreds of skills; loading every body to list them would be waste."""
    body = library.get("threat-hunt-evtx").body()
    assert "## Workflow" in body
    assert not body.startswith("---"), "frontmatter leaked into the instructions"


# --- classification ------------------------------------------------------


def test_offensive_skills_are_recognised(library: SkillCatalog):
    assert library.get("sliver-c2").posture == OFFENSIVE
    assert library.get("bloodhound-recon").posture == OFFENSIVE
    assert library.get("sliver-c2").is_offensive


def test_defensive_skills_are_recognised(library: SkillCatalog):
    for name in ("threat-hunt-evtx", "malware-triage", "ir-containment"):
        assert library.get(name).posture == DEFENSIVE, name


def test_a_skill_that_is_both_is_marked_dual_use(library: SkillCatalog):
    """Purple-team work is genuinely both, and flattening it either way is
    wrong: called defensive it gets applied automatically, called offensive it
    becomes unreachable."""
    assert library.get("purple-team-atk").posture == DUAL_USE


def test_an_unlabelled_skill_is_not_assumed_safe(library: SkillCatalog):
    """The failure that matters. Defaulting unlabelled content to defensive is
    exactly how offensive material ends up applied without anyone deciding to."""
    assert library.get("unlabelled").posture == DUAL_USE
    assert library.get("unlabelled").posture != DEFENSIVE


@pytest.mark.parametrize("domain,tags,expected", [
    ("red-teaming", ["exploitation"], OFFENSIVE),
    ("digital-forensics", ["forensics"], DEFENSIVE),
    ("", ["c2"], OFFENSIVE),
    ("", ["threat-hunting"], DEFENSIVE),
    ("", [], DUAL_USE),
])
def test_classification_reads_the_skills_own_metadata(domain, tags, expected):
    assert classify(domain, tags) == expected


# --- the line that matters ----------------------------------------------


def test_the_orchestrator_never_reaches_for_offensive_skills(library: SkillCatalog):
    """The operator may load anything; the orchestrator may apply almost
    nothing. Acquiring attack capability because a directory was on disk is
    not a behaviour anyone consented to."""
    auto = {s.name for s in library.auto_applicable()}
    assert "sliver-c2" not in auto
    assert "bloodhound-recon" not in auto
    assert "purple-team-atk" not in auto, "dual-use must not be automatic either"
    assert "unlabelled" not in auto


def test_defensive_skills_remain_automatic(library: SkillCatalog):
    """The gate must not be a blanket refusal — that would make the whole
    library dead weight."""
    auto = {s.name for s in library.auto_applicable()}
    assert auto == {"threat-hunt-evtx", "malware-triage", "ir-containment"}


def test_offensive_skills_stay_visible_and_readable(library: SkillCatalog):
    """Not automatic is not hidden. An operator cannot make a judgement about
    something they cannot see, and a tool that silently drops content is
    lying about what it loaded."""
    assert library.get("sliver-c2").body(), "instructions should still be readable"
    assert [s.name for s in library.search("c2")] == ["sliver-c2"]
    assert library.posture_counts()[OFFENSIVE] == 2, "the count must be reported honestly"


def test_counts_add_up(library: SkillCatalog):
    assert sum(library.posture_counts().values()) == len(library)


def test_search_matches_any_word(library: SkillCatalog):
    assert {s.name for s in library.search("malware forensics")} >= {"malware-triage"}
    assert library.search("") == []
