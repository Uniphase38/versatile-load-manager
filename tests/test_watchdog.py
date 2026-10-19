from datetime import datetime, timedelta

from victron_load_manager.watchdog import CommunicationWatchdog, WatchdogState


def test_startup_requires_two_healthy_cycles():
    watchdog = CommunicationWatchdog()
    now = datetime(2026, 10, 3, 12, 0)

    first = watchdog.success(now)
    assert first.current == WatchdogState.RECOVERING
    assert watchdog.control_allowed is False

    second = watchdog.success(now + timedelta(seconds=1))
    assert second.current == WatchdogState.OK
    assert watchdog.control_allowed is True


def test_three_consecutive_failures_enter_safe():
    watchdog = CommunicationWatchdog(safe_after_errors=3)
    now = datetime(2026, 10, 3, 12, 0)
    watchdog.success(now)
    watchdog.success(now + timedelta(seconds=1))

    first = watchdog.failure(now + timedelta(seconds=2), IOError("timeout"))
    second = watchdog.failure(now + timedelta(seconds=3), IOError("timeout"))
    third = watchdog.failure(now + timedelta(seconds=4), IOError("timeout"))

    assert first.current == WatchdogState.DEGRADED
    assert second.current == WatchdogState.DEGRADED
    assert third.current == WatchdogState.SAFE
    assert watchdog.control_allowed is False


def test_safe_recovers_only_after_two_complete_successes():
    watchdog = CommunicationWatchdog(safe_after_errors=1, recovery_cycles=2)
    now = datetime(2026, 10, 3, 12, 0)
    watchdog.failure(now, IOError("connection lost"))

    first = watchdog.success(now + timedelta(seconds=10))
    assert first.current == WatchdogState.RECOVERING
    assert watchdog.control_allowed is False

    second = watchdog.success(now + timedelta(seconds=11))
    assert second.current == WatchdogState.OK
    assert watchdog.control_allowed is True


def test_restart_is_error_count_based():
    watchdog = CommunicationWatchdog(
        safe_after_errors=3, restart_after_errors=10
    )
    now = datetime(2026, 10, 3, 12, 0)

    for index in range(9):
        transition = watchdog.failure(
            now + timedelta(seconds=index), IOError("timeout")
        )
        assert transition.restart_due is False

    tenth = watchdog.failure(now + timedelta(seconds=9), IOError("timeout"))
    assert tenth.current == WatchdogState.SAFE
    assert tenth.restart_due is True
