# Contributing to AETHER

Thanks for looking. This document is short on ceremony and specific about the
two or three things that are genuinely non-obvious here.

## Getting set up

```bash
git clone https://github.com/ansh2807/aether
cd aether
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -e ".[dev]"

pytest -q                       # should be all green, no keys needed
ruff check --select F,E9 src tests
```

No API keys are required to develop or to run the test suite. The default
config enables the bundled mock adapter, which is enough to exercise the whole
task lifecycle offline.

If you want to work against a real provider, `aether models` lists everything
that plugs in and marks what your machine can already reach.

## The one rule that matters: a test must be able to fail

This is the project norm most likely to get a PR sent back, so it is worth
stating outright.

A test that passes is not evidence. A test that passes **and would have failed
against the broken code** is evidence. Before you submit, break your own fix on
purpose and confirm the test goes red.

This is not hypothetical. While hardening the routing layer we wrote five
deliberate mutations of the fix and checked that each one was caught; the same
exercise elsewhere found a test asserting `circuit.failure_count >= 1` that a
later success silently reset, so it could never fail. Another found a container
sandbox that computed a "runtime not found" warning and then discarded it — a
silent downgrade of someone's isolation, caught only because the test asserted
on the warning rather than the exit code.

Practically:

- Assert on the thing that was actually wrong, not a proxy for it.
- Prefer asserting on observable state or artefacts over "it didn't raise".
- If a security test cannot run in an environment, make it **skip loudly**.
  A skipped isolation test must never be reported as a passing one — see
  `tests/test_verification_sandbox.py`, which distinguishes "the sandbox let
  the network through" from "the container never started".

## What good work looks like here

**Measure, don't assert.** Claims in commit messages should have numbers behind
them. "Parallel writes: 1 of 10 completions survived before, 10 of 10 now" is
useful; "improved concurrency" is not.

**Correct the record when you are wrong.** A previous roadmap item claimed the
context package was blowing past budget; measuring it showed 1816 tokens. The
right response was to fix the roadmap, not to add code that solved nothing.
Doing this in the open is welcome and will not be held against a PR.

**Do not fake integrations.** A provider without a real adapter reports
`ADAPTER_REQUIRED` and refuses. The mock agent is never routable while a real
provider is enabled, because a fabricated success standing in for a real
failure is the worst bug this project can have.

**Say what is not guaranteed.** [`docs/DURABILITY.md`](docs/DURABILITY.md)
lists guarantees with the test id that proves each one, and a section of things
that are explicitly *not* guaranteed. If you add a guarantee, add its test id.
If you find one that does not hold, that is a valuable issue.

## Adding a provider

Usually no code at all. Most hosted models speak the OpenAI
`/chat/completions` shape and most coding agents ship a CLI, and there is
already one adapter for each.

1. Add an entry to `src/aether/config/providers.py`.
2. Add the generated block to `config/aether.default.yaml`, **disabled**.
3. Leave `verified=False` unless you have actually called the endpoint from
   AETHER. `tests/test_provider_catalog.py` enforces this — the catalogue tells
   users which entries are documentation and which are proven, and that
   statement has to stay true.

A genuinely new protocol needs an adapter implementing `AgentAdapter`, plus
capability scores. It should not need orchestrator changes; if it does, say so
in the PR, because that is a design smell worth discussing.

## Pull requests

- Branch off `main`. CI runs the suite on Linux and Windows against Python 3.11
  and 3.12, plus a Postgres job, ruff, and the dashboard build.
- Windows is a first-class target. Console encoding, path handling and file
  locking all differ there and have all broken before.
- Explain the *why* in the commit message. The diff already shows the what.
- Small and focused beats large and comprehensive.

## Reporting bugs

Durability bugs are the most valuable thing you can report: cases where a task
was lost, silently duplicated, or marked complete when it was not. Include the
task id and, if you can, `aether task trace TASK-...`.

Before filing, run `aether doctor` and paste the output — it covers install,
providers, keys and permissions, and answers most environment questions
immediately.

## Security

Do not open a public issue for a vulnerability. See
[`SECURITY.md`](SECURITY.md) if present, or email the maintainer.

Note that verification runs agent-written code. It scrubs the environment to an
allowlist before doing so, because an earlier version passed the operator's
full environment — including every provider key on the machine — to code an
agent had just written. If you find a way around that scrubbing, please report
it privately.
