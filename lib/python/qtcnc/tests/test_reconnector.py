"""Unit tests for qtcnc.transport.reconnector.Reconnector.

These tests drive the Reconnector against a MockTransport using its
`simulate_ping_failure()` and `simulate_daemon_restart()` helpers. The
QTimer-driven paths are exercised by calling `_on_ping_timer` and
`_try_reconnect` directly so the suite stays deterministic without
spinning the Qt event loop. One end-to-end test does use the loop
(via short timers + processEvents) to verify the wiring is sound.
"""

from __future__ import annotations

import sys

import pytest

from qtpy.QtCore import QCoreApplication, QTimer
from qtpy.QtWidgets import QApplication

from qtcnc.core.status import Status
from qtcnc.transport.mock import MockTransport
from qtcnc.transport.reconnector import (
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_RECONNECTING,
    Reconnector,
)


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _process_events(timeout_ms: int = 50) -> None:
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.start(timeout_ms)
    while deadline.isActive():
        QCoreApplication.processEvents()


class _SignalSpy:
    """Lightweight signal recorder."""

    def __init__(self) -> None:
        self.events: list = []

    def __call__(self, *args) -> None:
        if not args:
            self.events.append(())
        elif len(args) == 1:
            self.events.append(args[0])
        else:
            self.events.append(tuple(args))


class TestInitialState:
    def test_starts_connected(self):
        t = MockTransport()
        r = Reconnector(t)
        assert r.state == STATE_CONNECTED
        assert r.missed == 0

    def test_invalid_ping_hz(self):
        t = MockTransport()
        with pytest.raises(ValueError):
            Reconnector(t, ping_hz=0)

    def test_invalid_miss_threshold(self):
        t = MockTransport()
        with pytest.raises(ValueError):
            Reconnector(t, miss_threshold=0)


class TestPingLoop:
    def test_successful_ping_resets_miss_counter(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t)
        # Pretend two pings already missed
        r._missed = 2
        r._on_ping_timer()
        assert r.missed == 0
        assert r.state == STATE_CONNECTED

    def test_ping_failure_increments_missed(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t, miss_threshold=3)
        t.simulate_ping_failure("stalled")
        r._on_ping_timer()
        assert r.missed == 1
        assert r.state == STATE_CONNECTED  # below threshold
        r._on_ping_timer()
        assert r.missed == 2
        assert r.state == STATE_CONNECTED

    def test_three_consecutive_failures_trigger_disconnect(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t, miss_threshold=3)
        spy = _SignalSpy()
        r.state_changed.connect(spy)
        t.simulate_ping_failure("stalled")
        r._on_ping_timer()
        r._on_ping_timer()
        r._on_ping_timer()
        assert r.state == STATE_DISCONNECTED
        assert STATE_DISCONNECTED in spy.events

    def test_partial_failures_recover_on_success(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t, miss_threshold=3)
        t.simulate_ping_failure("stalled")
        r._on_ping_timer()
        r._on_ping_timer()
        assert r.missed == 2
        # Recovery: clear the failure, ping succeeds.
        t.simulate_ping_failure("")
        r._on_ping_timer()
        assert r.missed == 0
        assert r.state == STATE_CONNECTED

    def test_skips_when_disconnected(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t)
        r._set_state(STATE_DISCONNECTED)
        # Should be a no-op; missed counter doesn't change even if ping
        # would fail.
        t.simulate_ping_failure("stalled")
        r._on_ping_timer()
        assert r.missed == 0

    def test_nonce_mismatch_treated_as_failure(self):
        """If the daemon returns a stale nonce three times, treat as miss."""

        class _BadNonce(MockTransport):
            def ping(self, nonce=None):
                # Always return the wrong nonce.
                return {"nonce": (nonce or 0) + 999}

        t = _BadNonce()
        t.hello()
        r = Reconnector(t, miss_threshold=3)
        for _ in range(3):
            r._on_ping_timer()
        assert r.state == STATE_DISCONNECTED


class TestReconnect:
    def test_successful_reconnect_returns_to_connected(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t)
        r._handle_disconnect("test")
        assert r.state == STATE_DISCONNECTED
        r._try_reconnect()
        assert r.state == STATE_CONNECTED
        assert r.missed == 0

    def test_failed_reconnect_doubles_backoff(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t, backoff_initial_s=1.0, backoff_max_s=10.0)
        r._handle_disconnect("test")

        # Make hello() raise so reconnect fails.
        original_hello = t.hello

        def _failing_hello():
            raise RuntimeError("daemon down")

        t.hello = _failing_hello

        assert r._backoff_s == 1.0
        r._try_reconnect()
        assert r.state == STATE_DISCONNECTED
        assert r._backoff_s == 2.0
        r._try_reconnect()
        assert r._backoff_s == 4.0
        r._try_reconnect()
        assert r._backoff_s == 8.0
        r._try_reconnect()
        assert r._backoff_s == 10.0  # capped
        r._try_reconnect()
        assert r._backoff_s == 10.0  # still capped

        # Recovery: hello succeeds again, state goes back to CONNECTED and
        # backoff resets.
        t.hello = original_hello
        r._try_reconnect()
        assert r.state == STATE_CONNECTED
        assert r._backoff_s == 1.0
        assert r._attempt == 0

    def test_reconnect_emits_attempt_count(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t)
        spy = _SignalSpy()
        r.reconnect_attempt.connect(spy)
        r._handle_disconnect("test")

        # Force two failures then a success.
        original_hello = t.hello
        t.hello = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        r._try_reconnect()
        r._try_reconnect()
        t.hello = original_hello
        r._try_reconnect()

        assert spy.events == [1, 2, 3]

    def test_reconnect_replays_subscribed_pins(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t)
        r.track_pin("halui.machine.is-on")
        r.track_pin("foo.bar")
        # Forge a previous subscription history then reconnect — the
        # MockTransport records subscribes in `_subscribed`.
        t._subscribed.clear()
        r._handle_disconnect("test")
        r._try_reconnect()
        assert "halui.machine.is-on" in t._subscribed
        assert "foo.bar" in t._subscribed

    def test_forget_pin_removes_from_replay(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t)
        r.track_pin("a")
        r.track_pin("b")
        r.forget_pin("a")
        assert r.subscribed_pins == frozenset({"b"})


class TestDaemonRestart:
    def test_restart_fires_signal_on_id_change(self):
        t = MockTransport()
        welcome = t.hello()
        r = Reconnector(t)
        r.record_initial_id(welcome["daemon_instance_id"])
        spy = _SignalSpy()
        r.daemon_restarted.connect(spy)

        t.simulate_daemon_restart()
        r._handle_disconnect("test")
        r._try_reconnect()

        assert spy.events == [()]
        assert r.daemon_instance_id == t._daemon_instance_id

    def test_no_restart_no_signal(self):
        t = MockTransport()
        welcome = t.hello()
        r = Reconnector(t)
        r.record_initial_id(welcome["daemon_instance_id"])
        spy = _SignalSpy()
        r.daemon_restarted.connect(spy)

        # Reconnect without rolling the id.
        r._handle_disconnect("test")
        r._try_reconnect()

        assert spy.events == []

    def test_initial_id_captured_silently(self):
        """A first reconnect with no prior baseline does NOT fire the signal."""
        t = MockTransport()
        r = Reconnector(t)
        # No record_initial_id → baseline is None.
        spy = _SignalSpy()
        r.daemon_restarted.connect(spy)
        t.simulate_daemon_restart()
        r._handle_disconnect("test")
        r._try_reconnect()
        assert spy.events == []
        assert r.daemon_instance_id is not None


class TestStartStop:
    def test_start_starts_ping_timer(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t)
        r.start()
        assert r._ping_timer.isActive()
        r.stop()
        assert not r._ping_timer.isActive()

    def test_stop_after_disconnect_cancels_reconnect(self):
        t = MockTransport()
        t.hello()
        r = Reconnector(t)
        r._handle_disconnect("test")
        assert r._reconnect_timer.isActive()
        r.stop()
        assert not r._reconnect_timer.isActive()

    def test_close_during_ping_stops(self):
        """If transport is closed mid-loop the Reconnector self-stops."""
        t = MockTransport()
        t.hello()
        r = Reconnector(t)
        r.start()
        t.close()
        r._on_ping_timer()
        assert r._stopped
        assert not r._ping_timer.isActive()


class TestStatusIntegration:
    def test_attach_routes_disconnect_through_status(self):
        t = MockTransport()
        t.hello()
        status = Status(t)
        status.bootstrap()
        r = Reconnector(t, status=status)
        status.attach_reconnector(r)

        disconnects: list[str] = []
        status.disconnected.connect(lambda reason: disconnects.append(reason))

        r._handle_disconnect("test")
        # state_changed → Status._on_reconnector_state → Status.disconnected
        assert disconnects == ["link lost"]

    def test_attach_routes_reconnect_attempts(self):
        t = MockTransport()
        t.hello()
        status = Status(t)
        status.bootstrap()
        r = Reconnector(t, status=status)
        status.attach_reconnector(r)

        attempts: list[int] = []
        status.reconnecting.connect(lambda n: attempts.append(n))

        # Simulate a failed reconnect: hello() raises so attempt count
        # increments without recovering.
        r._handle_disconnect("test")
        t.hello = lambda: (_ for _ in ()).throw(RuntimeError("nope"))
        r._try_reconnect()
        r._try_reconnect()
        assert attempts == [1, 2]

    def test_attach_routes_daemon_restarted(self):
        t = MockTransport()
        welcome = t.hello()
        status = Status(t)
        status.bootstrap()
        r = Reconnector(t, status=status)
        r.record_initial_id(welcome["daemon_instance_id"])
        status.attach_reconnector(r)

        seen: list = []
        status.daemon_restarted.connect(lambda: seen.append(True))

        t.simulate_daemon_restart()
        r._handle_disconnect("test")
        r._try_reconnect()
        assert seen == [True]

    def test_reconnect_replays_state_via_bootstrap(self):
        """After reconnect, the Status state matches the new daemon snapshot."""
        t = MockTransport()
        t.hello()
        status = Status(t)
        status.bootstrap()
        r = Reconnector(t, status=status)
        status.attach_reconnector(r)

        # Disconnect, change state on the (still-mock) daemon, reconnect.
        r._handle_disconnect("test")
        t.mutate_state(feed_rate=987.5)
        # The state diff fired during the disconnect would normally be
        # ignored by the client because the link is down — but MockTransport
        # fires synchronously. To prove the reconnect itself replays the
        # state, set the local Status state out of sync first.
        from dataclasses import replace as dc_replace
        status._state = dc_replace(status._state, feed_rate=0.0)

        r._try_reconnect()
        assert status.state.feed_rate == 987.5


class TestEndToEndTimers:
    """One real-timer test to prove the QTimer wiring works under an event loop."""

    def test_ping_timer_fires_under_event_loop(self):
        t = MockTransport()
        t.hello()
        # Very fast pings so the test is quick.
        r = Reconnector(t, ping_hz=200.0)
        r.start()
        try:
            _process_events(80)
            # Ping nonce should have advanced past 1 if the timer fired.
            assert r._next_nonce >= 1
            assert r.state == STATE_CONNECTED
        finally:
            r.stop()
