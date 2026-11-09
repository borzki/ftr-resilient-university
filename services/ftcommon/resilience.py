"""Software fault-tolerance patterns: timeouts, bounded retries with exponential
backoff and jitter, circuit breaker, and a resilient inter-service HTTP client."""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, TypeVar

import httpx

from . import config
from .logs import log_event, request_id_var
from .metrics import CIRCUIT_STATE, DEPENDENCY_CALLS, RETRIES

T = TypeVar("T")

CLOSED, HALF_OPEN, OPEN = "closed", "half_open", "open"
_STATE_VALUE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}


class CircuitBreaker:
    """Consecutive-failure circuit breaker (closed -> open -> half-open -> closed)."""

    def __init__(self, name: str, failure_threshold: int = 5, reset_timeout: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_progress = False
        self._publish()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == OPEN and self._clock() - self._opened_at >= self.reset_timeout:
                return HALF_OPEN
            return self._state

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == CLOSED:
                return True
            if self._state == OPEN:
                if self._clock() - self._opened_at < self.reset_timeout:
                    return False
                self._transition(HALF_OPEN)
            # half-open: allow a single trial request
            if self._trial_in_progress:
                return False
            self._trial_in_progress = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._trial_in_progress = False
            if self._state != CLOSED:
                self._transition(CLOSED)

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._trial_in_progress = False
            if self._state == HALF_OPEN or self._failures >= self.failure_threshold:
                self._opened_at = self._clock()
                if self._state != OPEN:
                    self._transition(OPEN)

    def _transition(self, new_state: str) -> None:
        old, self._state = self._state, new_state
        self._publish()
        log_event("circuit_breaker_transition", dependency=self.name, from_state=old, to_state=new_state)

    def _publish(self) -> None:
        CIRCUIT_STATE.labels(config.SERVICE_NAME, self.name).set(_STATE_VALUE[self._state])


class RetryableError(Exception):
    pass


def retry_call(fn: Callable[[], T], *, attempts: int, base_delay: float, max_delay: float = 1.0,
               retry_on: tuple[type[BaseException], ...] = (RetryableError,),
               on_retry: Callable[[int, BaseException], None] | None = None,
               sleep: Callable[[float], None] = time.sleep) -> T:
    """Call fn with bounded retries, exponential backoff and full jitter."""
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt == attempts:
                break
            if on_retry:
                on_retry(attempt, exc)
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            sleep(random.uniform(0, delay))
    assert last_exc is not None
    raise last_exc


class DependencyError(Exception):
    """A downstream dependency could not be used (timeout, unavailable, circuit open)."""

    def __init__(self, dependency: str, kind: str, detail: str = ""):
        super().__init__(f"{dependency}: {kind} {detail}".strip())
        self.dependency = dependency
        self.kind = kind


class _RetryableStatus(RetryableError):
    def __init__(self, response: httpx.Response):
        super().__init__(f"status {response.status_code}")
        self.response = response


class ServiceClient:
    """HTTP client for service-to-service calls.

    Baseline: no timeout, no retry, no circuit breaker (midterm behaviour).
    Resilient: per-call timeout, bounded retries (idempotent GETs only),
    circuit breaker, request-ID propagation and service-token authentication.
    """

    def __init__(self, dependency: str, base_url: str, *, attempts: int | None = None,
                 timeout_s: float | None = None, failure_threshold: int | None = None):
        self.dependency = dependency
        self.resilient = config.RESILIENT
        if self.resilient:
            t = timeout_s or config.HTTP_TIMEOUT_S
            timeout = httpx.Timeout(t, connect=min(config.HTTP_CONNECT_TIMEOUT_S, t))
            self.attempts = attempts or config.RETRY_ATTEMPTS
            self.breaker: CircuitBreaker | None = CircuitBreaker(
                dependency, failure_threshold or config.CB_FAILURE_THRESHOLD, config.CB_RESET_TIMEOUT_S)
        else:
            timeout = httpx.Timeout(None)
            self.attempts = 1
            self.breaker = None
        headers = {"X-Service-Token": config.SERVICE_TOKEN} if config.SERVICE_TOKEN else {}
        self.client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers,
                                   limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))

    def get(self, path: str) -> httpx.Response:
        svc = config.SERVICE_NAME
        if self.breaker and not self.breaker.allow_request():
            DEPENDENCY_CALLS.labels(svc, self.dependency, "short_circuit").inc()
            raise DependencyError(self.dependency, "circuit_open")

        def once() -> httpx.Response:
            resp = self.client.get(path, headers={"X-Request-ID": request_id_var.get()})
            if resp.status_code >= 500 and self.resilient:
                raise _RetryableStatus(resp)
            return resp

        def on_retry(attempt: int, exc: BaseException) -> None:
            RETRIES.labels(svc, self.dependency).inc()
            log_event("dependency_retry", dependency=self.dependency, attempt=attempt, reason=str(exc))

        try:
            if self.resilient:
                resp = retry_call(once, attempts=self.attempts, base_delay=config.RETRY_BASE_DELAY_S,
                                  retry_on=(_RetryableStatus, httpx.TransportError), on_retry=on_retry)
            else:
                resp = once()
                resp.raise_for_status()  # baseline: errors propagate to the caller
        except httpx.TimeoutException as exc:
            self._failed("timeout")
            raise DependencyError(self.dependency, "timeout", str(exc)) from exc
        except httpx.TransportError as exc:
            self._failed("unavailable")
            raise DependencyError(self.dependency, "unavailable", str(exc)) from exc
        except _RetryableStatus as exc:
            self._failed("bad_status")
            raise DependencyError(self.dependency, "bad_status", str(exc)) from exc
        if self.breaker:
            self.breaker.record_success()
        DEPENDENCY_CALLS.labels(svc, self.dependency, "success").inc()
        return resp

    def _failed(self, outcome: str) -> None:
        DEPENDENCY_CALLS.labels(config.SERVICE_NAME, self.dependency, outcome).inc()
        if self.breaker:
            self.breaker.record_failure()
