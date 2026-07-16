from __future__ import annotations

from app.observability.sentry import (
    compute_sentry_config,
    flush_sentry,
    initialize_sentry,
    report_worker_error,
)


class SentryVerificationError(RuntimeError):
    """Fixed, content-free exception used only for controlled verification."""


def main() -> int:
    config = compute_sentry_config(controlled_test=True)
    if not config.enabled:
        print("Sentry verification refused by environment or branch policy.")
        return 2
    if not initialize_sentry(controlled_test=True):
        print("Sentry verification could not initialize.")
        return 1

    event_id = report_worker_error(
        SentryVerificationError("M22E.2C Python worker Sentry verification"),
        feature="worker_runtime",
        operation="controlled_test",
        stage="verification",
        error_code="sentry_verification",
        source_platform="unknown",
        recoverable=False,
        terminal=True,
    )
    flush_sentry(timeout=2.0)
    if event_id is None:
        print("Sentry verification submission was not accepted.")
        return 1
    print("Sentry verification submitted one controlled event.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
