from __future__ import annotations

import unittest

from app.media_resolvers.circuit_breaker import ProviderCircuitBreaker


class ExternalCircuitBreakerTests(unittest.TestCase):
    def test_threshold_opens_and_success_resets(self) -> None:
        breaker = ProviderCircuitBreaker(threshold=2, window_seconds=120, open_seconds=60)
        self.assertTrue(breaker.allows("adapter", now=100))
        breaker.record_failure("adapter", now=100)
        self.assertTrue(breaker.allows("adapter", now=101))
        breaker.record_failure("adapter", now=102)
        self.assertFalse(breaker.allows("adapter", now=103))
        self.assertTrue(breaker.allows("adapter", now=163))
        breaker.record_failure("adapter", immediate=True, now=200)
        self.assertFalse(breaker.allows("adapter", now=201))
        breaker.record_success("adapter")
        self.assertTrue(breaker.allows("adapter", now=202))


if __name__ == "__main__":
    unittest.main()
