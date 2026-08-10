"""Circuit breaker and quota tests."""


from aether.core.circuit import CircuitBreaker
from aether.core.quota import QuotaManager
from aether.domain.enums import CircuitState, ProviderAvailability
from aether.config.settings import CircuitBreakerSettings, AetherSettings


def test_circuit_opens_after_threshold():
    cb = CircuitBreaker(CircuitBreakerSettings(failure_threshold=3, recovery_timeout_seconds=60))
    assert cb.allow("p1")
    cb.record_failure("p1")
    cb.record_failure("p1")
    assert cb.snapshot("p1").state == CircuitState.CLOSED
    cb.record_failure("p1")
    assert cb.snapshot("p1").state == CircuitState.OPEN
    assert not cb.allow("p1")


def test_circuit_half_open_after_cooldown():
    cb = CircuitBreaker(CircuitBreakerSettings(failure_threshold=1, recovery_timeout_seconds=0))
    cb.record_failure("p1")
    assert cb.snapshot("p1").state == CircuitState.OPEN
    # recovery_timeout 0 → immediate half-open
    assert cb.allow("p1")
    assert cb.snapshot("p1").state == CircuitState.HALF_OPEN
    cb.record_success("p1")
    assert cb.snapshot("p1").state == CircuitState.CLOSED


def test_quota_rate_limit(settings: AetherSettings):
    settings.quota.default_requests_per_minute = 2
    qm = QuotaManager(settings)
    assert qm.availability("mock") == ProviderAvailability.AVAILABLE
    qm.record_request("mock")
    qm.record_request("mock")
    assert qm.availability("mock") == ProviderAvailability.RATE_LIMITED


def test_quota_cooldown(settings: AetherSettings):
    qm = QuotaManager(settings)
    qm.set_cooldown("mock", 30)
    assert qm.availability("mock") == ProviderAvailability.RATE_LIMITED
