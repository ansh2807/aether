# What and why

<!-- The diff shows what changed. Use this space for why it was wrong before. -->

Fixes #

## How it was verified

<!--
Numbers where you have them. "Parallel writes: 1 of 10 completions survived
before, 10 of 10 after" is worth more than "improved concurrency".
-->

- [ ] `pytest -q` passes locally
- [ ] `ruff check --select F,E9 src tests` is clean
- [ ] **I broke the fix on purpose and watched the new test fail.**

<!--
That third box is the one that matters. A test that passes is not evidence; a
test that passes and would have failed against the broken code is. If you
skipped it, say so rather than ticking it — an honest gap is fine, a false tick
is not.
-->

## Anything reviewers should push back on

<!--
Assumptions you were unsure about, a shortcut you took, a case you did not
cover. Naming it here is faster than having it found in review, and it will not
be held against the PR.
-->

## Checklist

- [ ] Windows and Linux both considered (encoding, paths, file locking have all broken here before)
- [ ] If this adds a durability guarantee, `docs/DURABILITY.md` cites its test id
- [ ] If this adds a provider, it is **disabled** in the default config and `verified=False` unless the endpoint was actually called
- [ ] No secrets, tokens or personal email addresses in the diff or commit metadata
