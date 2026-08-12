---
name: secure-code-review
description: Reviews agent-written code for vulnerabilities before a task can complete. Detects hardcoded secrets, injection sinks, unsafe deserialization, weak crypto and disabled TLS verification, then asks a model to judge the cases that need context. Defensive only.
domain: application-security
subdomain: secure-code-review
tags: [sast, secrets, owasp, cwe, code-review, defensive]
cwe: [CWE-78, CWE-89, CWE-79, CWE-94, CWE-95, CWE-502, CWE-327, CWE-295, CWE-798, CWE-611]
owasp_top_10: [A01, A02, A03, A05, A07, A08]
nist_csf: [PR.DS, PR.PS, DE.CM]
version: 1.0.0
license: Apache-2.0
---

# Secure code review

## When to use

Automatically, as a verification check, whenever a task produced source files.
AETHER's whole premise is that "the agent said done" is not evidence — and until
now the artifacts were checked for *existence and syntax*, never for whether
they were dangerous. An agent that writes `subprocess.run(cmd, shell=True)` with
a user-controlled string passes a syntax check perfectly.

## Scope, and why it is narrow

**Defensive only.** This finds weaknesses in code the orchestrator's own agents
just wrote, in the operator's own workspace. It does not scan remote targets,
probe networks, or carry exploitation tooling. AETHER is a general-purpose
orchestrator that people point at their own repositories; offensive capability
in that position is a liability, not a feature.

## Prerequisites

None. The deterministic pass needs no model and no network, so it still runs
when every provider is down. A model, if one is routable, is used only for the
judgement calls the patterns cannot make.

## Workflow

1. **Deterministic pass.** Regex detectors over changed files, each mapped to a
   CWE. This is the floor: fast, offline, reproducible, and it cannot be talked
   out of a finding.
2. **Contextual pass.** For findings whose severity depends on context — is that
   string actually user-controlled? — a model is asked to judge, through the
   normal router, so it works with whatever provider is available.
3. **Gate.** `HIGH` findings fail verification. Lower severities are recorded
   and reported but do not block.

## Verification

A finding names the file, the line, the CWE, and the specific reason. A check
that says "possible security issue" without saying where is not actionable and
is treated as a bug in this skill.

## Known limits

Regex detectors have false positives and miss anything obfuscated; this is a
first line, not an audit. It reviews **code the agents wrote**, not the
dependencies they pulled in, and it does not replace a real SAST tool or a
human reviewer on security-critical work.
