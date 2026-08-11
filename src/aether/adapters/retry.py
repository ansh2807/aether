"""Retry-with-backoff for adapter execution.

Retries live at the ADAPTER layer, not the orchestrator. The orchestrator
only ever sees one final ``ExecutionResult`` per attempt, so a retry-exhausted
call still costs exactly one circuit-breaker failure and one failover
decision -- not one per try. Wiring retries here (instead of in
``core/orchestrator.py``) is what makes that true.

Design note -- hand-rolled instead of ``tenacity``:
``tenacity`` is a declared dependency and is a fine choice in general, but our
"should this be retried" signal is a field on a normal return value
(``ExecutionResult.failure_class``), not a raised exception, and the retry
count is a per-call, config-driven value that must be able to fully disable
the machinery (``network_retries: 0`` runs the call exactly once, no
tenacity ``Retrying`` object involved at all). Tenacity supports
result-based predicates via ``retry_if_result`` / ``AsyncRetrying``, but
expressing "retry on this field, with a dynamic stop count, cleanly
short-circuited when disabled, using a module-level base delay tests can
monkeypatch" ends up being about the same number of lines either way -- and
the hand-rolled loop is easier to read and step through, especially with a
failure-class predicate as the actual point of the exercise. If a future
adapter's failures surface as exceptions instead, reaching for tenacity
there is reasonable.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional

from aether.domain.enums import FailureClass
from aether.domain.models import ExecutionResult
from aether.observability.logging import get_logger

log = get_logger("aether.adapters.retry")

# Failure classes where retrying the same request has a real chance of
# succeeding. Deliberately excludes AUTH, QUOTA, POLICY_DENIED, CONTEXT and
# CANCELLED -- those are permanent for this attempt, and retrying AUTH or
# QUOTA can make things worse (provider-side lockouts, burned quota).
RETRYABLE_FAILURE_CLASSES = frozenset({FailureClass.NETWORK, FailureClass.TIMEOUT})

# Base delay for exponential backoff, in seconds. Module-level so tests can
# monkeypatch it down and keep the suite fast.
BASE_DELAY_SECONDS = 0.05

# Upper bound on any single backoff wait, regardless of attempt number.
MAX_DELAY_SECONDS = 2.0


def is_retryable(failure_class: Optional[FailureClass]) -> bool:
    """Whether a failure of this class is worth retrying."""
    return failure_class in RETRYABLE_FAILURE_CLASSES


def backoff_delay(attempt: int) -> float:
    """Full-jitter exponential backoff for the given 0-indexed retry attempt."""
    ceiling = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2**attempt))
    return random.uniform(0, ceiling)


async def execute_with_retry(
    call: Callable[[], Awaitable[ExecutionResult]],
    retries: int,
    *,
    provider_id: str = "",
) -> ExecutionResult:
    """Run ``call``, retrying with backoff while the failure looks transient.

    ``retries`` is the number of ADDITIONAL attempts allowed after the first,
    so ``retries=2`` means up to 3 total attempts. ``retries <= 0`` disables
    retrying entirely: ``call`` runs exactly once and its result -- success
    or failure -- is returned as-is.
    """
    attempts_allowed = max(0, retries)
    attempt = 0
    while True:
        result = await call()
        if result.success or not is_retryable(result.failure_class) or attempt >= attempts_allowed:
            return result

        delay = backoff_delay(attempt)
        log.info(
            "adapter_retry",
            provider=provider_id,
            attempt=attempt + 1,
            of=attempts_allowed,
            failure_class=str(result.failure_class),
            delay_seconds=round(delay, 3),
        )
        if delay > 0:
            await asyncio.sleep(delay)
        attempt += 1
