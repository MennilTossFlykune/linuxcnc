"""End-to-end tests for ZmqClientTransport + ZmqServerTransport over TCP.

A thin mirror of `test_zmq_transport.py`: the protocol paths are already
exhaustively tested there over `ipc://`, so this file concentrates on
the scenarios where TCP differs from ipc — endpoint binding, cross-host
(well, cross-localhost) delivery, and the fact that `_split_endpoint`
needs port+1 to be free.

The real goal: catch regressions in the TCP code path without waiting
for a human to run a remote client against a real daemon. If any of
these tests fail, the `scripts/qtcnc-launch --bind tcp://...` flow is
broken and `docs/qtcnc-remote.md` has a bad day.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from dataclasses import replace
from typing import Any

import pytest

zmq = pytest.importorskip("zmq")

from qtcnc import PROTOCOL_VERSION
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.state import StateStore
from qtcnc.core.types import (
    ErrorMessage,
    ErrorSeverity,
    MachineState,
    Position,
    TaskMode,
    TaskState,
)
from qtcnc.signals import CommandVerb, Lifecycle
from qtcnc.transport.base import DeclarePinsResult, NackError
from qtcnc.transport.zmq_client import ZmqClientTransport
from qtcnc.transport.zmq_server import ZmqServerTransport


# TCP's SUB-side subscription propagates slower than ipc — the default
# 0.1s used by `test_zmq_transport.py` works here too but we lift it
# slightly to absorb slow CI.
_SUB_SETUP_DELAY = 0.2


# ---------------------------------------------------------------------------
# Fake handler — mirrors _FakeHandler in test_zmq_transport.py. The
# duplication is deliberate: if that file's handler gains scenario-specific
# behavior, the TCP tests should not silently inherit it. Keep the TCP path
# exercising a minimal, self-contained handler.
# ---------------------------------------------------------------------------


class _FakeHandler:
    def __init__(self) -> None:
        self._state = StateStore(
            connected=True,
            task_state=TaskState.ESTOP,
            machine=MachineState(estop=True, powered=False),
        )
        self.commands: list[tuple[CommandVerb, dict[str, Any]]] = []
        self.loaded_programs: list[str] = []
        self.declared: list[HalPinSpec] = []
        self.pin_writes: list[tuple[str, Any]] = []
        self.subscribed_pins: list[str] = []
        self._pin_specs: dict[str, HalPinSpec] = {}
        self._locked = False

    @property
    def state(self) -> StateStore:
        return self._state

    def set_state(self, new: StateStore) -> None:
        self._state = new

    def on_hello(self) -> dict[str, Any]:
        return {"protocol_version": PROTOCOL_VERSION, "daemon": "tcp-fake"}

    def on_get_snapshot(self) -> StateStore:
        return self._state

    def on_exec_command(self, verb: CommandVerb, kwargs: dict[str, Any]) -> None:
        self.commands.append((verb, dict(kwargs)))
        if verb == CommandVerb.STATE_ESTOP_RESET:
            self._state = replace(
                self._state,
                machine=replace(self._state.machine, estop=False),
                task_state=TaskState.ESTOP_RESET,
            )

    def on_load_program(self, path: str) -> None:
        self.loaded_programs.append(path)

    def on_declare_pins(self, specs: list[HalPinSpec]) -> DeclarePinsResult:
        if self._locked:
            for spec in specs:
                existing = self._pin_specs.get(spec.name)
                if existing is None:
                    raise NackError("hal_locked")
            return DeclarePinsResult(
                created=[s.name for s in specs], inherited=True,
            )
        for spec in specs:
            self._pin_specs[spec.name] = spec
            self.declared.append(spec)
        self._locked = True
        return DeclarePinsResult(
            created=[s.name for s in specs], inherited=False,
        )

    def on_write_pin(self, name: str, value: Any) -> None:
        if name not in self._pin_specs:
            raise NackError(f"unknown pin: {name}")
        self.pin_writes.append((name, value))

    def on_subscribe_pin(self, name: str) -> None:
        self.subscribed_pins.append(name)


# ---------------------------------------------------------------------------
# Port allocation — `_split_endpoint` derives the REP port as `port+1`, so
# "bind to 0 and read back" is awkward. Grab two adjacent free ports with
# a helper and feed the first one in.
# ---------------------------------------------------------------------------


def _find_two_adjacent_free_ports() -> int:
    """Return N such that N and N+1 are both free TCP ports on localhost.

    Small race window between here and the actual bind, acceptable for
    pytest runs. Retries up to 20 times before giving up.
    """
    for _ in range(20):
        s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s1.bind(("127.0.0.1", 0))
            port1 = s1.getsockname()[1]
            try:
                s2.bind(("127.0.0.1", port1 + 1))
                return port1
            except OSError:
                continue
        finally:
            s1.close()
            s2.close()
    raise RuntimeError("could not find adjacent free TCP ports after 20 tries")


class _ServerThread(threading.Thread):
    def __init__(self, server: ZmqServerTransport) -> None:
        super().__init__(daemon=True, name="test-qtcnc-serverd-tcp")
        self._server = server
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._server.serve_once(100)
            except zmq.ZMQError:
                return
            except Exception:
                return

    def shutdown(self) -> None:
        self._stop_event.set()
        self.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tcp_endpoint() -> str:
    port = _find_two_adjacent_free_ports()
    return f"tcp://127.0.0.1:{port}"


@pytest.fixture
def transport_pair(tcp_endpoint: str):
    handler = _FakeHandler()
    server = ZmqServerTransport(tcp_endpoint, handler)
    server_thread = _ServerThread(server)
    server_thread.start()
    client = ZmqClientTransport(tcp_endpoint, request_timeout_ms=2000)
    try:
        yield client, server, handler
    finally:
        try:
            client.close()
        except Exception:
            pass
        server_thread.shutdown()
        try:
            server.close()
        except Exception:
            pass


def _pump_until(client: ZmqClientTransport, condition, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        client.pump_sub(50)
        if condition():
            return True
    return False


# ---------------------------------------------------------------------------
# Endpoint plumbing
# ---------------------------------------------------------------------------


class TestTcpEndpoint:
    def test_server_splits_tcp_port_plus_one(self, tcp_endpoint):
        handler = _FakeHandler()
        server = ZmqServerTransport(tcp_endpoint, handler)
        try:
            port = int(tcp_endpoint.rsplit(":", 1)[1])
            assert server.pub_endpoint == f"tcp://127.0.0.1:{port}"
            assert server.cmd_endpoint == f"tcp://127.0.0.1:{port + 1}"
        finally:
            server.close()

    def test_server_rejects_tcp_without_port(self):
        with pytest.raises(ValueError, match="tcp endpoint must include a port"):
            ZmqServerTransport("tcp://127.0.0.1", _FakeHandler())


# ---------------------------------------------------------------------------
# REQ/REP — same shape as test_zmq_transport.TestHello, now over TCP.
# ---------------------------------------------------------------------------


class TestHello:
    def test_hello_welcome_roundtrip_over_tcp(self, transport_pair):
        client, _, _ = transport_pair
        reply = client.hello()
        assert reply["daemon"] == "tcp-fake"
        assert client.is_connected is True


class TestGetSnapshot:
    def test_snapshot_roundtrip_over_tcp(self, transport_pair):
        client, _, handler = transport_pair
        handler.set_state(replace(
            handler.state,
            position=Position(x=3.5, y=-1.25, z=0.75),
            feed_rate=120.0,
        ))
        client.hello()
        snap = client.get_snapshot()
        assert snap.position.x == pytest.approx(3.5)
        assert snap.position.y == pytest.approx(-1.25)
        assert snap.position.z == pytest.approx(0.75)
        assert snap.feed_rate == pytest.approx(120.0)


class TestExecCommand:
    def test_state_estop_reset_round_trip_over_tcp(self, transport_pair):
        client, _, handler = transport_pair
        client.hello()
        client.exec_command(CommandVerb.STATE_ESTOP_RESET)
        assert handler.commands[-1][0] == CommandVerb.STATE_ESTOP_RESET
        snap = client.get_snapshot()
        assert snap.machine.estop is False


class TestDeclarePins:
    def test_declare_pins_round_trip_over_tcp(self, transport_pair):
        client, _, handler = transport_pair
        client.hello()
        specs = [
            HalPinSpec(
                name="qtcnc.dro_x.value-out",
                type=HalType.FLOAT,
                dir=HalDir.OUT,
            ),
        ]
        result = client.declare_pins(specs)
        assert result.created == ["qtcnc.dro_x.value-out"]
        assert result.inherited is False
        assert len(handler.declared) == 1


class TestSubscribePin:
    def test_subscribe_pin_records_on_server(self, transport_pair):
        client, _, handler = transport_pair
        client.hello()
        client.subscribe_pin("halui.machine.is-on")
        # REQ/REP is synchronous — by the time the call returns, the
        # server handler has logged the subscription.
        assert "halui.machine.is-on" in handler.subscribed_pins


# ---------------------------------------------------------------------------
# PUB/SUB — the slow-joiner problem is more pronounced on TCP than ipc,
# so we use _pump_until to give SUB time to catch up.
# ---------------------------------------------------------------------------


class TestPubSubOverTcp:
    def test_state_diff_published_and_received(self, transport_pair):
        client, server, _ = transport_pair
        seen: list[list[tuple[str, Any]]] = []
        client.set_on_state_diff(seen.append)
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        server.publish_state_diff([("feed_rate", 120.0)])

        assert _pump_until(
            client, lambda: bool(seen), timeout_s=3.0,
        ), "no state diff received over TCP"
        assert seen[0] == [("feed_rate", 120.0)]

    def test_state_snapshot_published_as_diff(self, transport_pair):
        client, server, handler = transport_pair
        seen: list[list[tuple[str, Any]]] = []
        client.set_on_state_diff(seen.append)
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        handler.set_state(replace(
            handler.state,
            position=Position(x=9.0, y=8.0, z=7.0),
            feed_rate=200.0,
        ))
        server.publish_state_snapshot(handler.state)

        assert _pump_until(
            client, lambda: bool(seen), timeout_s=3.0,
        ), "no snapshot received over TCP"
        # A snapshot is fanned out as a diff of every field — check the
        # two we mutated are present.
        changes = {k: v for k, v in seen[-1]}
        assert changes["position"] == Position(x=9.0, y=8.0, z=7.0)
        assert changes["feed_rate"] == 200.0

    def test_error_published_and_received(self, transport_pair):
        client, server, _ = transport_pair
        errors: list[ErrorMessage] = []
        client.set_on_error(lambda e: errors.append(e))
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        msg = ErrorMessage(
            severity=ErrorSeverity.OPERATOR_ERROR,
            text="tcp path smoke",
            timestamp=time.time(),
        )
        server.publish_error(msg)

        assert _pump_until(
            client, lambda: bool(errors), timeout_s=3.0,
        ), "no error update received over TCP"
        assert errors[-1].text == "tcp path smoke"
