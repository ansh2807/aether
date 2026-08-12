"""Secure code review of what the agents just wrote.

Verification checked that artifacts exist and that Python parses. Neither
notices that an agent wrote `subprocess.run(user_input, shell=True)` — that
file exists and compiles perfectly. "Verification before trust" was doing half
its job: proving the work happened, never asking whether it was safe.

Two passes, deliberately in this order:

**Deterministic first.** Regex detectors, each mapped to a CWE. Offline,
reproducible, and they still run when every provider is down — the same reason
routing and staffing are deterministic. A model cannot argue this pass out of a
finding.

**Model second, and only for judgement.** Whether `shell=True` is exploitable
depends on where the string came from, which a regex cannot know. That question
goes to whatever provider the router picks, so it works across the whole pool.

Defensive only. This reads the operator's own workspace to find weaknesses in
code the orchestrator itself produced. It has no remote scanning and no
exploitation tooling; AETHER is pointed at people's own repositories, and
offensive capability in that position is a liability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Severity drives the gate: HIGH fails a task, the rest are reported.
HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"

# Files worth reading. Everything else is noise or binary.
REVIEWABLE_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rb", ".php", ".java",
    ".sh", ".bash", ".ps1", ".yaml", ".yml", ".tf", ".sql",
})

# Never read these: vendored code is not what the agent wrote, and scanning it
# buries real findings under thousands of third-party matches.
SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".tox", ".mypy_cache", ".pytest_cache", "vendor", "site-packages",
})

MAX_FILE_BYTES = 400_000
MAX_FILES = 400


@dataclass(frozen=True)
class Finding:
    rule: str
    cwe: str
    severity: str
    file: str
    line: int
    excerpt: str
    why: str

    def as_line(self) -> str:
        return f"{self.severity} {self.cwe} {self.file}:{self.line} — {self.why}"


@dataclass(frozen=True)
class Rule:
    name: str
    cwe: str
    severity: str
    pattern: re.Pattern[str]
    why: str
    # Judgement calls: flagged, but the severity depends on whether the input is
    # attacker-controlled, which needs a model or a human to settle.
    needs_context: bool = False


def _p(expr: str) -> re.Pattern[str]:
    return re.compile(expr, re.IGNORECASE)


RULES: tuple[Rule, ...] = (
    # --- secrets. Highest confidence, highest cost when missed. -----------
    Rule("aws_access_key", "CWE-798", HIGH,
         _p(r"\bAKIA[0-9A-Z]{16}\b"),
         "AWS access key id committed in source"),
    Rule("private_key", "CWE-798", HIGH,
         _p(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
         "private key material committed in source"),
    Rule("bearer_secret", "CWE-798", HIGH,
         _p(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})"),
         "provider API token committed in source"),
    Rule("hardcoded_password", "CWE-798", MEDIUM,
         _p(r"""(?:password|passwd|secret|api_key|apikey)\s*[:=]\s*["'][^"'{}$\s]{8,}["']"""),
         "credential assigned from a literal rather than the environment",
         needs_context=True),

    # --- injection --------------------------------------------------------
    Rule("shell_injection", "CWE-78", HIGH,
         _p(r"(?:subprocess\.(?:run|call|Popen|check_output)\([^)]*shell\s*=\s*True"
            r"|\bos\.system\s*\(|\bexec\s+[\"']?\$)"),
         "shell invoked with a composed string; command injection if any part is user-controlled",
         needs_context=True),
    Rule("sql_injection", "CWE-89", HIGH,
         _p(r"""(?:execute|executemany|query)\s*\(\s*(?:f["']|["'][^"']*["']\s*(?:%|\+|\.format))"""),
         "SQL built by string formatting instead of bound parameters",
         needs_context=True),
    Rule("code_eval", "CWE-95", HIGH,
         _p(r"\b(?:eval|exec)\s*\(|\bnew\s+Function\s*\(|\bFunction\s*\(\s*[\"']"),
         "dynamic code execution; arbitrary code if the input is not fully trusted",
         needs_context=True),
    Rule("unsafe_deserialization", "CWE-502", HIGH,
         _p(r"\b(?:pickle|cPickle|dill|marshal)\.loads?\s*\(|yaml\.load\s*\((?![^)]*Safe)"),
         "deserialising untrusted data can execute arbitrary code",
         needs_context=True),
    Rule("xss_sink", "CWE-79", MEDIUM,
         _p(r"\.innerHTML\s*=|dangerouslySetInnerHTML|document\.write\s*\("),
         "unescaped HTML sink; cross-site scripting if the value is user-supplied",
         needs_context=True),
    Rule("xxe", "CWE-611", MEDIUM,
         _p(r"etree\.(?:parse|fromstring)\s*\(|xml\.dom\.minidom\.parse"),
         "XML parsed without disabling external entities",
         needs_context=True),

    # --- transport and crypto --------------------------------------------
    Rule("tls_verification_disabled", "CWE-295", HIGH,
         _p(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false"
            r"|InsecureSkipVerify\s*:\s*true|ssl\._create_unverified_context"),
         "TLS certificate verification disabled; traffic is interceptable"),
    Rule("weak_hash", "CWE-327", MEDIUM,
         _p(r"\bhashlib\.(?:md5|sha1)\s*\(|\bMD5\s*\(|CryptoJS\.(?:MD5|SHA1)"),
         "broken hash function; unusable for passwords, signatures or integrity",
         needs_context=True),
    Rule("weak_random_for_secrets", "CWE-338", MEDIUM,
         _p(r"\brandom\.(?:random|randint|choice)\s*\([^)]*\)\s*(?:#.*)?$"),
         "predictable RNG; use `secrets` for anything security-relevant",
         needs_context=True),

    # --- exposure ---------------------------------------------------------
    Rule("debug_enabled", "CWE-489", MEDIUM,
         _p(r"\bdebug\s*=\s*True|app\.run\([^)]*debug\s*=\s*True|FLASK_DEBUG\s*=\s*1"),
         "debug mode ships a console and stack traces to users"),
    Rule("bind_all_interfaces", "CWE-668", MEDIUM,
         _p(r"""["']0\.0\.0\.0["']|host\s*=\s*["']0\.0\.0\.0["']"""),
         "binds every interface; reachable beyond localhost",
         needs_context=True),
)


def _walk(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        if path.suffix.lower() not in REVIEWABLE_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


_SUPPRESSIONS = ("aether: allow-insecure", "nosec")


def _is_suppressed(line: str, preceding_comments: str = "") -> bool:
    """Honour an explicit, reasoned suppression on the line or its comment block.

    The comment block, not just the line above: a reason worth writing rarely
    fits beside the code. AETHER's own `PRAGMA busy_timeout` suppression is two
    comment lines followed by the call, and a one-line lookback still missed
    it — which teaches people the feature is broken and to disable the whole
    rule instead.
    """
    return any(t in line or t in preceding_comments for t in _SUPPRESSIONS)


_COMMENT = re.compile(r"^\s*(?:#|//|\*|/\*|--)")
_FENCE = re.compile(r'"""|\'\'\'')


def _code_lines(text: str) -> Iterable[tuple[int, str]]:
    """Yield only lines that are actually code.

    Found by running this scanner on AETHER itself: two of its three findings
    were in *this file* — one matching the module docstring's example of
    `shell=True`, one matching a rule's own regex. Prose that describes an
    insecure pattern is not an insecure pattern, and a scanner that blocks a
    build on its own documentation is a scanner people switch off.

    Docstring tracking is a line-level approximation, not a parser: it costs
    nothing, works across languages, and the failure mode is a missed finding
    inside a docstring rather than a false alarm.
    """
    in_block = False
    for lineno, line in enumerate(text.splitlines(), 1):
        fences = len(_FENCE.findall(line))
        if in_block:
            if fences:
                in_block = False
            continue
        if fences % 2 == 1:
            # Opens a docstring that does not close on this line. Anything
            # before the fence is still code worth scanning.
            in_block = True
            head = line.split('"""')[0].split("'''")[0]
            if head.strip() and not _COMMENT.match(line):
                yield lineno, head
            continue
        if _COMMENT.match(line):
            continue
        yield lineno, line


def scan_workspace(root: Path | str, limit: int = MAX_FILES) -> list[Finding]:
    """Deterministic pass. No model, no network, no configuration."""
    root = Path(root)
    findings: list[Finding] = []
    if not root.exists():
        return findings

    for count, path in enumerate(_walk(root)):
        if count >= limit:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)

        # This module defines the patterns it looks for, so scanning it finds
        # every rule in itself. Excluded by path, not by a rule exception.
        if path.resolve() == Path(__file__).resolve():
            continue

        raw = text.splitlines()
        for lineno, line in _code_lines(text):
            # Walk back over the contiguous comment block above this line.
            block: list[str] = []
            idx = lineno - 2
            while idx >= 0 and _COMMENT.match(raw[idx]):
                block.append(raw[idx])
                idx -= 1
            if len(line) > 2000 or _is_suppressed(line, "\n".join(block)):
                # Minified bundles produce noise, not signal.
                continue
            for rule in RULES:
                if rule.pattern.search(line):
                    findings.append(Finding(
                        rule=rule.name, cwe=rule.cwe, severity=rule.severity,
                        file=rel, line=lineno,
                        excerpt=line.strip()[:160], why=rule.why,
                    ))
    return findings


def blocking(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == HIGH]


def summarise(findings: list[Finding]) -> str:
    if not findings:
        return "no findings"
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    parts = [f"{counts[s]} {s.lower()}" for s in (HIGH, MEDIUM, LOW) if s in counts]
    top = "; ".join(f.as_line() for f in findings[:3])
    return f"{', '.join(parts)} — {top}"


def review_prompt(findings: list[Finding], goal: str = "") -> str:
    """Ask a model to settle the findings whose severity depends on context.

    Only the `needs_context` ones. Sending the whole list would invite the model
    to re-litigate a hardcoded AWS key, which is not a judgement call.
    """
    contextual = {r.name for r in RULES if r.needs_context}
    judgeable = [f for f in findings if f.rule in contextual]
    if not judgeable:
        return ""

    lines = "\n".join(
        f"{i}. [{f.cwe}] {f.file}:{f.line} — {f.why}\n   {f.excerpt}"
        for i, f in enumerate(judgeable[:20], 1)
    )
    return (
        "You are reviewing code that an automated agent just wrote"
        f"{f' for: {goal}' if goal else ''}.\n\n"
        "For each finding below, decide whether it is genuinely exploitable "
        "*in this code*, or a false positive because the input is a constant, "
        "a test fixture, or otherwise not attacker-controlled.\n\n"
        f"{lines}\n\n"
        "Answer one line per finding: `<number>: EXPLOITABLE|FALSE_POSITIVE|UNCLEAR "
        "— one sentence of reasoning`. Be strict: if you cannot trace where the "
        "value comes from, say UNCLEAR rather than guessing."
    )


def parse_review(reply: str, findings: list[Finding]) -> dict[int, str]:
    """Read the model's verdicts back. Anything unparseable stays UNCLEAR."""
    verdicts: dict[int, str] = {}
    for match in re.finditer(
        r"^\s*(\d+)\s*[:.)-]\s*(EXPLOITABLE|FALSE_POSITIVE|UNCLEAR)",
        reply or "", re.IGNORECASE | re.MULTILINE,
    ):
        verdicts[int(match.group(1))] = match.group(2).upper()
    return verdicts


def apply_verdicts(findings: list[Finding], verdicts: dict[int, str]) -> list[Finding]:
    """Downgrade only what the model positively cleared.

    A missing or unparseable verdict leaves the finding as it was. Silence must
    never clear a finding — that would make an unreachable provider look like a
    clean bill of health.
    """
    contextual = {r.name for r in RULES if r.needs_context}
    judgeable = [f for f in findings if f.rule in contextual]
    cleared = {
        id(judgeable[i - 1])
        for i, verdict in verdicts.items()
        if verdict == "FALSE_POSITIVE" and 1 <= i <= len(judgeable)
    }
    out: list[Finding] = []
    for f in findings:
        if id(f) in cleared:
            out.append(Finding(f.rule, f.cwe, LOW, f.file, f.line, f.excerpt,
                               f"{f.why} (model judged not exploitable)"))
        else:
            out.append(f)
    return out
