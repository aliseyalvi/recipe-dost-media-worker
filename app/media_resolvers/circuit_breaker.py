from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class ProviderCircuitBreaker:
    def __init__(
        self,
        *,
        threshold: int = 5,
        window_seconds: int = 120,
        open_seconds: int = 60,
    ) -> None:
        self._threshold = threshold
        self._window_seconds = window_seconds
        self._open_seconds = open_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._open_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def allows(self, adapter_id: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            opened_until = self._open_until.get(adapter_id, 0.0)
            if opened_until <= current:
                self._open_until.pop(adapter_id, None)
                return True
            return False

    def record_success(self, adapter_id: str) -> None:
        with self._lock:
            self._failures.pop(adapter_id, None)
            self._open_until.pop(adapter_id, None)

    def record_failure(
        self,
        adapter_id: str,
        *,
        immediate: bool = False,
        now: float | None = None,
    ) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            failures = self._failures[adapter_id]
            while failures and current - failures[0] > self._window_seconds:
                failures.popleft()
            failures.append(current)
            if immediate or len(failures) >= self._threshold:
                self._open_until[adapter_id] = current + self._open_seconds


provider_circuit_breaker = ProviderCircuitBreaker()
