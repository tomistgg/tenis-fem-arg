"""Bounded HTTP retry policy with jitter and structured terminal failures."""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import requests

from pipeline_errors import SourceRequestError
from run_state import IssueSeverity, record_run_issue

DEFAULT_TIMEOUT = (10.0, 30.0)
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


class SlidingWindowRateLimiter:
    """Keep calls within a maximum count for every rolling time window."""

    def __init__(
        self,
        max_calls: int,
        period_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        if period_seconds <= 0:
            raise ValueError("period_seconds must be greater than 0")
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._call_times: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                cutoff = now - self.period_seconds
                while self._call_times and self._call_times[0] <= cutoff:
                    self._call_times.popleft()

                if len(self._call_times) < self.max_calls:
                    self._call_times.append(now)
                    return

                delay = self._call_times[0] + self.period_seconds - now

            self._sleeper(max(0.0, delay))


def request_with_retry(
    method: str,
    url: str,
    *,
    component: str,
    attempts: int = 4,
    timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
    backoff_base: float = 0.75,
    backoff_cap: float = 8.0,
    jitter_ratio: float = 0.35,
    failure_status: IssueSeverity = "partial",
    before_attempt: Callable[[], None] | None = None,
    requester: Callable[..., requests.Response] = requests.request,
    **kwargs: Any,
) -> requests.Response:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    kwargs.pop("timeout", None)
    last_error: BaseException | None = None
    attempts_used = 0
    for attempt in range(1, attempts + 1):
        attempts_used = attempt
        try:
            if before_attempt is not None:
                before_attempt()
            response = requester(method, url, timeout=timeout, **kwargs)
            if response.status_code not in RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(
                f"retryable HTTP {response.status_code}", response=response
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
        except requests.HTTPError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code not in RETRYABLE_STATUS_CODES:
                break

        if attempt < attempts:
            delay = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
            time.sleep(delay + random.uniform(0.0, delay * jitter_ratio))

    error = SourceRequestError(
        component=component,
        operation=f"HTTP {method.upper()}",
        message=f"request failed after {attempts_used} attempts: {url}",
        context={"url": url, "attempts": attempts_used, "cause": str(last_error or "unknown")},
        retryable=True,
    )
    record_run_issue(component, error, severity=failure_status)
    raise error from last_error


def get_with_retry(url: str, **kwargs: Any) -> requests.Response:
    return request_with_retry("GET", url, **kwargs)
