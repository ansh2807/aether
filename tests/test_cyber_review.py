"""The cyber skill: reviewing the code AETHER's own agents wrote.

Verification proved artifacts existed and Python parsed. Neither notices that
`subprocess.run(cmd, shell=True)` compiles perfectly. "Verification before
trust" was only asking whether the work happened, never whether it was safe.

Scope is deliberately narrow and these tests pin that too: the skill reads the
operator's own workspace and carries no remote scanning or exploitation
tooling. AETHER is pointed at people's own repositories.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aether.skills.cyber import (
    apply_verdicts,
    blocking,
    parse_review,
    review_prompt,
    scan_workspace,
    summarise,
)
from aether.skills.cyber.review import HIGH, LOW, RULES


def _write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --- it finds the things that matter -------------------------------------


@pytest.mark.parametrize("body,rule", [
    ("import subprocess\nsubprocess.run(cmd, shell=True)\n", "shell_injection"),
    ("KEY = 'AKIAIOSFODNN7EXAMPLE'\n", "aws_access_key"),
    ("TOKEN = 'sk-abcdefghijklmnopqrstuvwxyz123'\n", "bearer_secret"),
    ("requests.get(url, verify=False)\n", "tls_verification_disabled"),
    ("import pickle\npickle.loads(blob)\n", "unsafe_deserialization"),
    ("cur.execute(f'SELECT * FROM t WHERE id={uid}')\n", "sql_injection"),
    ("eval(user_input)\n", "code_eval"),
    ("el.innerHTML = value\n", "xss_sink"),
    ("import hashlib\nhashlib.md5(b'x')\n", "weak_hash"),
    ("app.run(debug=True)\n", "debug_enabled"),
])
def test_each_detector_fires(tmp_path: Path, body: str, rule: str):
    _write(tmp_path, "sample.py", body)
    found = {f.rule for f in scan_workspace(tmp_path)}
    assert rule in found, f"{rule} missed in {body!r}"


def test_clean_code_produces_nothing(tmp_path: Path):
    """A scanner that flags everything gets ignored, which is the same as not
    having one."""
    _write(tmp_path, "ok.py",
           "import subprocess\n"
           "def add(a, b):\n"
           "    return a + b\n"
           "def run(args: list[str]):\n"
           "    return subprocess.run(args, shell=False, check=True)\n")
    assert scan_workspace(tmp_path) == []


def test_every_finding_is_actionable(tmp_path: Path):
    """A finding that says "possible security issue" without a location is a
    bug in this skill, per SKILL.md."""
    _write(tmp_path, "bad.py", "import os\nos.system(cmd)\n")
    for f in scan_workspace(tmp_path):
        assert f.file and f.line > 0
        assert f.cwe.startswith("CWE-")
        assert len(f.why) > 20, "the reason must explain, not label"
        assert f.excerpt


def test_every_rule_carries_a_cwe():
    for rule in RULES:
        assert rule.cwe.startswith("CWE-"), rule.name
        assert rule.severity in {"HIGH", "MEDIUM", "LOW"}


# --- and does not drown you ----------------------------------------------


def test_vendored_code_is_not_scanned(tmp_path: Path):
    """Third-party code is not what the agent wrote, and scanning it buries
    real findings under thousands of matches."""
    _write(tmp_path, "node_modules/lib/x.js", "el.innerHTML = v; eval(q)\n")
    _write(tmp_path, ".venv/lib/y.py", "import pickle\npickle.loads(b)\n")
    assert scan_workspace(tmp_path) == []


def test_binary_and_unknown_types_are_skipped(tmp_path: Path):
    _write(tmp_path, "notes.md", "run `eval(x)` to break things\n")
    _write(tmp_path, "data.bin", "AKIAIOSFODNN7EXAMPLE\n")
    assert scan_workspace(tmp_path) == []


def test_minified_lines_are_skipped(tmp_path: Path):
    _write(tmp_path, "bundle.js", "var x=1;" * 400 + "el.innerHTML=v\n")
    assert scan_workspace(tmp_path) == []


def test_an_explicit_suppression_is_honoured(tmp_path: Path):
    """An operator who has reasoned about a line should be able to say so —
    otherwise they silence the whole check instead."""
    _write(tmp_path, "a.py", "import os\nos.system(fixed)  # aether: allow-insecure\n")
    assert scan_workspace(tmp_path) == []


def test_symlinks_are_not_followed(tmp_path: Path):
    """The same hole the verification engine closed: a symlink turns a
    workspace scan into a host-file read."""
    secret = tmp_path / "outside.py"
    secret.write_text("TOKEN = 'sk-abcdefghijklmnopqrstuvwxyz123'\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        (ws / "link.py").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this platform")
    assert scan_workspace(ws) == []


# --- the gate ------------------------------------------------------------


def test_high_severity_blocks_and_medium_does_not(tmp_path: Path):
    _write(tmp_path, "high.py", "requests.get(u, verify=False)\n")
    _write(tmp_path, "med.py", "import hashlib\nhashlib.md5(b'x')\n")
    findings = scan_workspace(tmp_path)
    severe = blocking(findings)
    assert {f.severity for f in severe} == {HIGH}
    assert len(severe) < len(findings), "medium findings should not block"


def test_verification_fails_a_task_that_ships_a_secret(tmp_path: Path):
    """End to end: the gate has to actually be wired, not just importable."""
    import asyncio

    from aether.core.verification import VerificationEngine
    from aether.domain.enums import VerificationStatus
    from aether.domain.models import Task

    ws = tmp_path / "ws"
    _write(ws, "leak.py", "AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n")

    class _Recorder:
        async def save_result(self, result):
            pass

    task = Task(original_prompt="build a thing", workspace_path=str(ws))
    result = asyncio.run(VerificationEngine(repo=_Recorder()).verify_task(task))

    review = [c for c in result.checks if c.name == "secure_code_review"]
    assert review, "the security check never ran"
    assert review[0].status == VerificationStatus.FAILED
    assert result.status == VerificationStatus.FAILED, (
        "a task shipping a hardcoded AWS key was allowed to complete"
    )


def test_verification_passes_clean_code(tmp_path: Path):
    import asyncio

    from aether.core.verification import VerificationEngine
    from aether.domain.enums import VerificationStatus
    from aether.domain.models import Task

    ws = tmp_path / "ws"
    _write(ws, "ok.py", "def add(a, b):\n    return a + b\n")

    class _Recorder:
        async def save_result(self, result):
            pass

    task = Task(original_prompt="build a thing", workspace_path=str(ws))
    result = asyncio.run(VerificationEngine(repo=_Recorder()).verify_task(task))
    review = [c for c in result.checks if c.name == "secure_code_review"]
    assert review and review[0].status == VerificationStatus.PASSED


# --- the model pass ------------------------------------------------------


def test_only_judgement_calls_are_sent_to_a_model(tmp_path: Path):
    """A hardcoded AWS key is not a judgement call; inviting a model to
    re-litigate it only creates a way to talk the finding away."""
    _write(tmp_path, "a.py",
           "KEY = 'AKIAIOSFODNN7EXAMPLE'\nimport os\nos.system(cmd)\n")
    prompt = review_prompt(scan_workspace(tmp_path))
    assert "CWE-78" in prompt, "the contextual finding was not sent"
    assert "AKIAIOSFODNN7EXAMPLE" not in prompt
    assert "CWE-798" not in prompt


def test_no_judgement_calls_means_no_model_round_trip(tmp_path: Path):
    _write(tmp_path, "a.py", "KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    assert review_prompt(scan_workspace(tmp_path)) == ""


def test_a_model_can_clear_a_false_positive(tmp_path: Path):
    _write(tmp_path, "a.py", "import os\nos.system('ls -la')\n")
    findings = scan_workspace(tmp_path)
    assert blocking(findings)

    verdicts = parse_review("1: FALSE_POSITIVE — the command is a constant", findings)
    after = apply_verdicts(findings, verdicts)
    assert not blocking(after)
    assert after[0].severity == LOW


def test_silence_from_the_model_never_clears_a_finding(tmp_path: Path):
    """An unreachable provider must not read as a clean bill of health."""
    _write(tmp_path, "a.py", "import os\nos.system(cmd)\n")
    findings = scan_workspace(tmp_path)

    for reply in ("", "the service is unavailable", "1: EXPLOITABLE", "1: UNCLEAR"):
        after = apply_verdicts(findings, parse_review(reply, findings))
        assert blocking(after), f"reply {reply!r} silently cleared a HIGH finding"


def test_verdicts_for_nonexistent_findings_are_ignored(tmp_path: Path):
    """A model that invents finding 47 must not corrupt the list."""
    _write(tmp_path, "a.py", "import os\nos.system(cmd)\n")
    findings = scan_workspace(tmp_path)
    after = apply_verdicts(findings, parse_review("47: FALSE_POSITIVE — nope", findings))
    assert blocking(after)


# --- scope ---------------------------------------------------------------


def test_the_skill_reaches_nothing_but_the_local_filesystem():
    """Pinned deliberately. AETHER is pointed at people's own repositories, so
    remote scanning or exploitation tooling in this module would be a
    liability. This is the test that should fail if someone adds it.

    Checks imports rather than substrings: an earlier version grepped for the
    word "exploit" and failed on the docstring explaining that severity depends
    on whether a finding is *exploitable*. Prose is not capability; imports are.
    """
    import ast

    source = (Path(__file__).resolve().parent.parent
              / "src" / "aether" / "skills" / "cyber" / "review.py").read_text(encoding="utf-8")

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    network = {"socket", "http", "urllib", "requests", "httpx", "ftplib",
               "telnetlib", "paramiko", "scapy", "nmap", "asyncio"}
    assert not (imported & network), (
        f"the review skill imports {sorted(imported & network)} — it should only "
        f"read the local filesystem"
    )
    assert imported <= {"re", "dataclasses", "pathlib", "typing", "__future__"}, (
        f"unexpected imports: {sorted(imported)}"
    )


def test_summary_leads_with_the_worst(tmp_path: Path):
    _write(tmp_path, "a.py",
           "import hashlib\nhashlib.md5(b'x')\nrequests.get(u, verify=False)\n")
    text = summarise(scan_workspace(tmp_path))
    assert "high" in text
    assert summarise([]) == "no findings"


def test_a_suppression_applies_to_its_whole_comment_block(tmp_path: Path):
    """A reason worth writing rarely fits beside the code.

    AETHER's own `PRAGMA busy_timeout` suppression is two comment lines above
    the call. Same-line-only, and then one-line lookback, both still flagged
    it — which teaches people the feature is broken and to disable the rule.
    """
    _write(tmp_path, "a.py",
           "import os\n"
           "# aether: allow-insecure — the command is a fixed constant and\n"
           "# cannot be influenced by user input.\n"
           "os.system('ls -la')\n")
    assert scan_workspace(tmp_path) == []


def test_a_comment_block_without_a_marker_suppresses_nothing(tmp_path: Path):
    """Otherwise any commented code would silence the check below it."""
    _write(tmp_path, "a.py",
           "import os\n# just explaining what this does\nos.system(cmd)\n")
    assert scan_workspace(tmp_path)


def test_prose_describing_an_insecure_pattern_is_not_a_finding(tmp_path: Path):
    """Found by dogfooding: two of the scanner's first three findings on
    AETHER were in its own docstring and rule definitions."""
    _write(tmp_path, "doc.py",
           '"""Never write subprocess.run(cmd, shell=True) — it is injectable.\n\n'
           'Also avoid el.innerHTML = value.\n"""\n\n'
           "def safe():\n    return 1\n")
    assert scan_workspace(tmp_path) == []


def test_the_scanner_is_clean_against_aethers_own_source():
    """Dogfooding as a test. A scanner that cannot pass on the repo that ships
    it will be switched off by the first person who runs it."""
    src = Path(__file__).resolve().parent.parent / "src"
    findings = scan_workspace(src)
    assert not blocking(findings), (
        "AETHER's own source trips the gate: "
        + "; ".join(f.as_line() for f in blocking(findings)[:5])
    )
