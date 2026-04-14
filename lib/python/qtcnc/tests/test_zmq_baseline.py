"""Tests for A1: baseline snapshot re-publish + tunable HWM + drop counter.

Covers three behaviours introduced in v2 A1:

1. `QtcncServer` re-publishes a full `state.snapshot` every
   `snapshot_interval_s` even when nothing has changed, so slow joiners
   and drifted subscribers have a reference point.
2. `ZmqServerTransport` counts `state_diff` send failures (PUB HWM full)
   in a `state_diff_dropped` counter, and non-state-diff publishes do
   not bump the counter.
3. The daemon's `WELCOME` reply exposes `metrics = {state_diff_dropped,
   snapshot_interval_s}` so clients can observe daemon health.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

import pytest

zmq = pytest.importorskip("zmq")

from qtcnc.core.state import StateStore
from qtcnc.core.types import ErrorMessage, ErrorSeverity
from qtcnc.server import QtcncServer
from qtcnc.signals import MessageType, Topic
from qtcnc.transport.base import DeclarePinsResult
from qtcnc.transport.codec import decode_envelope
from qtcnc.transport.zmq_client import ZmqClientTransport
from qtcnc.transport.zmq_server import ZmqServerTransport

from qtcnc.tests.test_server import (
    _FakeHalModule,
    _FakeLinuxcncModule,
    _ServerThread,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def endpoint() -> str:
    base = f"ipc:///tmp/qtcnc-baseline-{uuid.uuid4().hex[:12]}"
    yield base
    for suffix in (".pub", ".cmd"):
        path = base[len("ipc://"):] + suffix
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test 1: periodic baseline snapshot
# ---------------------------------------------------------------------------


class TestBaselineSnapshot:
    def test_snapshot_republished_periodically(self, endpoint: str) -> None:
        """With no state changes, the daemon should still emit snapshots
        at `snapshot_interval_s` intervals so slow joiners get a baseline."""
        lc = _FakeLinuxcncModule()
        halmod = _FakeHalModule()
        server = QtcncServer(
            endpoint,
            linuxcnc_module=lc,
            hal_module=halmod,
            poll_interval_s=0.02,
            snapshot_interval_s=0.15,
        )
        server_thread = _ServerThread(server)
        server_thread.start()

        ctx = zmq.Context()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, Topic.STATE_SNAPSHOT.encode())
        sub.setsockopt(zmq.RCVTIMEO, 2000)
        sub.connect(endpoint + ".pub")
        # Wait for subscription to propagate and for the poll thread to
        # run at least one tick so the first baseline fires.
        time.sleep(0.1)

        received: list[bytes] = []
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline and len(received) < 2:
            try:
                parts = sub.recv_multipart()
            except zmq.Again:
                break
            if len(parts) == 2 and parts[0] == Topic.STATE_SNAPSHOT.encode():
                received.append(parts[1])

        try:
            sub.close(linger=0)
            ctx.term()
        finally:
            server_thread.shutdown()
            try:
                server.stop()
            except Exception:
                pass

        assert len(received) >= 2, (
            f"expected >= 2 baseline snapshots within 2.5s with interval 0.15s,"
            f" got {len(received)}"
        )
        # Decode one to make sure it's a well-formed SNAPSHOT envelope.
        env = decode_envelope(received[0])
        assert env.type == MessageType.SNAPSHOT
        assert isinstance(env.payload.get("snapshot"), dict)


# ---------------------------------------------------------------------------
# Test 2: drop counter
# ---------------------------------------------------------------------------


class _NullHandler:
    """Handler stub for ZmqServerTransport — we never drive serve_once."""

    def on_hello(self) -> dict[str, Any]:
        return {}

    def on_get_snapshot(self) -> StateStore:
        return StateStore()

    def on_exec_command(self, verb: Any, kwargs: dict[str, Any]) -> None:
        pass

    def on_load_program(self, path: str) -> None:
        pass

    def on_declare_pins(self, specs: Any) -> DeclarePinsResult:
        return DeclarePinsResult(created=[], inherited=False)

    def on_write_pin(self, name: str, value: Any) -> None:
        pass

    def on_subscribe_pin(self, name: str) -> None:
        pass


class TestDropCounter:
    def test_state_diff_dropped_increments_on_send_failure(
        self, endpoint: str,
    ) -> None:
        """ZMQ PUB sockets typically drop silently when HWM is reached,
        so rather than flooding the socket (non-deterministic), we
        monkey-patch `send_multipart` to always raise `zmq.Again` and
        verify the drop counter tracks the failures."""
        server = ZmqServerTransport(endpoint, _NullHandler())
        try:
            def _raise_again(*args: Any, **kwargs: Any) -> None:
                raise zmq.Again()

            server._pub.send_multipart = _raise_again  # type: ignore[assignment]

            assert server.state_diff_dropped == 0

            server.publish_state_diff([("feed_rate", 120.0)])
            assert server.state_diff_dropped == 1

            server.publish_state_diff([("feed_rate", 60.0)])
            assert server.state_diff_dropped == 2

            # Non-state-diff publishes must NOT bump the counter. They
            # each fail (the monkey-patch still raises), but only
            # state_diff is tracked.
            server.publish_state_snapshot(StateStore())
            server.publish_hal_pin("qtcnc.dro_x.value-out", 1.25)
            server.publish_error(ErrorMessage(
                severity=ErrorSeverity.NML_ERROR,
                text="boom",
                timestamp=0.0,
            ))
            server.publish_lifecycle("custom_event", {"k": "v"})

            assert server.state_diff_dropped == 2
        finally:
            try:
                server.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Test 3: WELCOME metrics
# ---------------------------------------------------------------------------


class TestWelcomeMetrics:
    def test_welcome_reports_metrics_block(self, endpoint: str) -> None:
        lc = _FakeLinuxcncModule()
        halmod = _FakeHalModule()
        server = QtcncServer(
            endpoint,
            linuxcnc_module=lc,
            hal_module=halmod,
            poll_interval_s=0.05,
            snapshot_interval_s=7.5,
            sndhwm=4242,
        )
        server_thread = _ServerThread(server)
        server_thread.start()
        client = ZmqClientTransport(endpoint, request_timeout_ms=2000)
        try:
            reply = client.hello()
            assert "metrics" in reply, f"WELCOME missing metrics: {reply}"
            metrics = reply["metrics"]
            assert "state_diff_dropped" in metrics
            assert "snapshot_interval_s" in metrics
            assert metrics["state_diff_dropped"] == 0
            assert metrics["snapshot_interval_s"] == pytest.approx(7.5)
        finally:
            try:
                client.close()
            except Exception:
                pass
            server_thread.shutdown()
            try:
                server.stop()
            except Exception:
                pass
