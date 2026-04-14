"""End-to-end tests for ZmqClientTransport + ZmqServerTransport.

Each test spins a real ZmqServerTransport in a background thread, a real
ZmqClientTransport connected to the same ipc:// endpoint, and drives the
protocol over actual ZMQ sockets. No mocking. If pyzmq is unavailable
the whole module is skipped.
"""

from __future__ import annotations

import os
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
from qtcnc.transport.base import DeclarePinsResult, NackError, TransportError
from qtcnc.transport.zmq_client import ZmqClientTransport
from qtcnc.transport.zmq_server import ZmqServerTransport


# ---------------------------------------------------------------------------
# Fake daemon-side handler
# ---------------------------------------------------------------------------


class _FakeHandler:
    """In-memory ZmqServerHandler used to back the server transport."""

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

    # ---- state helpers for the test to drive ----

    @property
    def state(self) -> StateStore:
        return self._state

    def set_state(self, new: StateStore) -> None:
        self._state = new

    # ---- ZmqServerHandler protocol ----

    def on_hello(self) -> dict[str, Any]:
        return {"protocol_version": PROTOCOL_VERSION, "daemon": "fake"}

    def on_get_snapshot(self) -> StateStore:
        return self._state

    def on_exec_command(self, verb: CommandVerb, kwargs: dict[str, Any]) -> None:
        self.commands.append((verb, dict(kwargs)))
        if verb == CommandVerb.ESTOP_RESET:
            self._state = replace(
                self._state,
                machine=replace(self._state.machine, estop=False),
                task_state=TaskState.ESTOP_RESET,
            )
        elif verb == CommandVerb.POWER_ON:
            if self._state.machine.estop:
                raise NackError("cannot power on while estopped")
            self._state = replace(
                self._state,
                machine=replace(self._state.machine, powered=True),
                task_state=TaskState.ON,
            )

    def on_load_program(self, path: str) -> None:
        self.loaded_programs.append(path)

    def on_declare_pins(self, specs: list[HalPinSpec]) -> DeclarePinsResult:
        if self._locked:
            # Inheritance path — every spec must match exactly.
            for spec in specs:
                existing = self._pin_specs.get(spec.name)
                if existing is None:
                    raise NackError("hal_locked")
                if existing.type != spec.type or existing.dir != spec.dir:
                    raise NackError(f"pin_type_mismatch:{spec.name}")
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
        if self._pin_specs[name].dir == HalDir.IN:
            raise NackError(f"cannot write IN pin: {name}")
        self.pin_writes.append((name, value))

    def on_subscribe_pin(self, name: str) -> None:
        self.subscribed_pins.append(name)


# ---------------------------------------------------------------------------
# Server thread helper
# ---------------------------------------------------------------------------


class _ServerThread(threading.Thread):
    """Drives server.serve_once in a tight loop until stopped.

    (Note: `stop` / `_stop` are reserved by `threading.Thread` internals,
    so this class uses `shutdown` and `_stop_event`.)
    """

    def __init__(self, server: ZmqServerTransport) -> None:
        super().__init__(daemon=True, name="test-qtcnc-serverd")
        self._server = server
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._server.serve_once(100)
            except zmq.ZMQError:
                return
            except Exception:
                # Swallow — tests verify outcomes on the client side.
                return

    def shutdown(self) -> None:
        self._stop_event.set()
        self.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def endpoint() -> str:
    base = f"ipc:///tmp/qtcnc-test-{uuid.uuid4().hex[:12]}"
    yield base
    # ZMQ leaves the ipc socket files behind. Clean them up.
    for suffix in (".pub", ".cmd"):
        path = base[len("ipc://"):] + suffix
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def transport_pair(endpoint: str):
    handler = _FakeHandler()
    server = ZmqServerTransport(endpoint, handler)
    server_thread = _ServerThread(server)
    server_thread.start()
    client = ZmqClientTransport(endpoint, request_timeout_ms=2000)
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


def _wait_for(condition, timeout: float = 2.0, poll: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(poll)
    return False


def _pump_until(client: ZmqClientTransport, condition, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        client.pump_sub(50)
        if condition():
            return True
    return False


# ---------------------------------------------------------------------------
# REQ/REP tests
# ---------------------------------------------------------------------------


class TestHello:
    def test_hello_welcome_roundtrip(self, transport_pair):
        client, _, _ = transport_pair
        reply = client.hello()
        assert reply["protocol_version"] == list(PROTOCOL_VERSION) or reply[
            "protocol_version"
        ] == list(PROTOCOL_VERSION)
        assert reply["daemon"] == "fake"
        assert client.is_connected is True

    def test_hello_dispatches_connected(self, transport_pair):
        client, _, _ = transport_pair
        fired: list[bool] = []
        client.set_on_connected(lambda: fired.append(True))
        client.hello()
        assert fired == [True]


class TestGetSnapshot:
    def test_snapshot_roundtrip(self, transport_pair):
        client, _, handler = transport_pair
        handler.set_state(replace(
            handler.state,
            position=Position(x=1.25, y=-2.0, z=4.5),
            feed_rate=60.0,
        ))
        client.hello()
        snap = client.get_snapshot()
        assert isinstance(snap, StateStore)
        assert snap.position.x == pytest.approx(1.25)
        assert snap.position.y == pytest.approx(-2.0)
        assert snap.position.z == pytest.approx(4.5)
        assert snap.feed_rate == pytest.approx(60.0)
        assert snap.task_state == TaskState.ESTOP


class TestExecCommand:
    def test_estop_reset_round_trip(self, transport_pair):
        client, _, handler = transport_pair
        client.hello()
        client.exec_command(CommandVerb.ESTOP_RESET)
        assert handler.commands[-1][0] == CommandVerb.ESTOP_RESET
        snap = client.get_snapshot()
        assert snap.machine.estop is False

    def test_set_mode_encodes_enum(self, transport_pair):
        client, _, handler = transport_pair
        client.hello()
        client.exec_command(CommandVerb.SET_MODE, mode=TaskMode.MDI)
        verb, kwargs = handler.commands[-1]
        assert verb == CommandVerb.SET_MODE
        # Server decoded TaskMode from its int form back into the enum.
        assert kwargs["mode"] == TaskMode.MDI

    def test_nack_on_invalid_transition(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        with pytest.raises(NackError) as exc:
            client.exec_command(CommandVerb.POWER_ON)
        assert "estopped" in exc.value.reason

    def test_nack_on_unknown_verb(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        # Use the low-level _request so we can send a bogus verb string.
        from qtcnc.signals import MessageType
        with pytest.raises(NackError) as exc:
            client._request(
                MessageType.EXEC_COMMAND,
                {"verb": "no_such_verb", "kwargs": {}},
            )
        assert "unknown verb" in exc.value.reason


class TestLoadProgram:
    def test_load_program_roundtrip(self, transport_pair):
        client, _, handler = transport_pair
        client.hello()
        client.load_program("/tmp/part.ngc")
        assert handler.loaded_programs == ["/tmp/part.ngc"]


class TestDeclarePins:
    def test_declare_pins_roundtrip(self, transport_pair):
        client, _, handler = transport_pair
        client.hello()
        specs = [
            HalPinSpec(
                name="qtcnc.dro_x.value-out",
                type=HalType.FLOAT,
                dir=HalDir.OUT,
            ),
            HalPinSpec(
                name="qtcnc.estop.led",
                type=HalType.BIT,
                dir=HalDir.IN,
            ),
        ]
        result = client.declare_pins(specs)
        assert result.created == [
            "qtcnc.dro_x.value-out",
            "qtcnc.estop.led",
        ]
        assert result.inherited is False
        # Server-side received specs should be typed HalPinSpecs, not dicts.
        assert len(handler.declared) == 2
        assert handler.declared[0].name == "qtcnc.dro_x.value-out"
        assert handler.declared[0].type == HalType.FLOAT
        assert handler.declared[0].dir == HalDir.OUT
        assert handler.declared[1].type == HalType.BIT
        assert handler.declared[1].dir == HalDir.IN

    def test_second_declare_pins_matching_inherits(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        specs = [
            HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT),
            HalPinSpec(name="qtcnc.b", type=HalType.BIT, dir=HalDir.IN),
        ]
        first = client.declare_pins(specs)
        assert first.inherited is False
        assert first.created == ["qtcnc.a", "qtcnc.b"]

        # A "second client" redeclaring the same specs joins the session.
        second = client.declare_pins(specs)
        assert second.inherited is True
        assert second.created == ["qtcnc.a", "qtcnc.b"]

    def test_second_declare_pins_new_pin_nacks_hal_locked(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        client.declare_pins([
            HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT),
        ])
        with pytest.raises(NackError) as exc:
            client.declare_pins([
                HalPinSpec(name="qtcnc.b", type=HalType.FLOAT, dir=HalDir.OUT),
            ])
        assert exc.value.reason == "hal_locked"

    def test_second_declare_pins_type_mismatch_nacks(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        client.declare_pins([
            HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT),
        ])
        with pytest.raises(NackError) as exc:
            client.declare_pins([
                HalPinSpec(name="qtcnc.a", type=HalType.BIT, dir=HalDir.OUT),
            ])
        assert exc.value.reason.startswith("pin_type_mismatch")

    def test_second_declare_pins_dir_mismatch_nacks(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        client.declare_pins([
            HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT),
        ])
        with pytest.raises(NackError) as exc:
            client.declare_pins([
                HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.IN),
            ])
        assert exc.value.reason.startswith("pin_type_mismatch")


class TestWritePin:
    def test_write_pin_after_declare(self, transport_pair):
        client, _, handler = transport_pair
        client.hello()
        client.declare_pins([
            HalPinSpec(name="qtcnc.dro.value-out", type=HalType.FLOAT, dir=HalDir.OUT),
        ])
        client.write_pin("qtcnc.dro.value-out", 3.75)
        assert handler.pin_writes == [("qtcnc.dro.value-out", 3.75)]

    def test_write_pin_unknown_nacks(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        with pytest.raises(NackError):
            client.write_pin("qtcnc.ghost", 1.0)


class TestSubscribePin:
    def test_subscribe_pin_roundtrip(self, transport_pair):
        client, _, handler = transport_pair
        client.hello()
        client.subscribe_pin("halui.machine.is-on")
        assert handler.subscribed_pins == ["halui.machine.is-on"]


class TestBye:
    def test_close_sends_bye_and_disconnects(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        fired: list[str] = []
        client.set_on_disconnected(fired.append)
        client.close()
        assert fired == ["closed"]
        assert client.is_connected is False

    def test_close_is_idempotent(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        client.close()
        client.close()  # must not raise


class TestClosedTransportRaises:
    def test_request_after_close_raises(self, transport_pair):
        client, _, _ = transport_pair
        client.hello()
        client.close()
        from qtcnc.transport.base import TransportClosed
        with pytest.raises(TransportClosed):
            client.get_snapshot()


# ---------------------------------------------------------------------------
# PUB/SUB tests
# ---------------------------------------------------------------------------


# Subscription propagation over ipc:// is nearly instant but not zero.
# Give the SUB a brief moment after HELLO before the server publishes.
_SUB_SETUP_DELAY = 0.1


class TestPublishStateDiff:
    def test_state_diff_dispatches_typed_values(self, transport_pair):
        client, server, handler = transport_pair
        seen: list[list[tuple[str, Any]]] = []
        client.set_on_state_diff(seen.append)
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        new_position = Position(x=2.5, y=3.0, z=4.0)
        server.publish_state_diff([("position", new_position)])

        assert _pump_until(client, lambda: bool(seen))
        changes = seen[0]
        assert len(changes) == 1
        name, value = changes[0]
        assert name == "position"
        # The wire round-trip must land in a typed Position dataclass.
        assert isinstance(value, Position)
        assert value.x == pytest.approx(2.5)
        assert value.y == pytest.approx(3.0)
        assert value.z == pytest.approx(4.0)

    def test_state_diff_dispatches_scalar(self, transport_pair):
        client, server, _ = transport_pair
        seen: list[Any] = []
        client.set_on_state_diff(seen.append)
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        server.publish_state_diff([("feed_rate", 120.0)])
        assert _pump_until(client, lambda: bool(seen))
        changes = seen[0]
        assert changes == [("feed_rate", 120.0)]

    def test_state_diff_dispatches_tuple(self, transport_pair):
        client, server, _ = transport_pair
        seen: list[Any] = []
        client.set_on_state_diff(seen.append)
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        server.publish_state_diff([("active_gcodes", (20, 90, 17))])
        assert _pump_until(client, lambda: bool(seen))
        changes = seen[0]
        assert changes[0][0] == "active_gcodes"
        # tuple[int, ...] round-trips as a typed tuple of ints
        assert changes[0][1] == (20, 90, 17)
        assert isinstance(changes[0][1], tuple)


class TestPublishHalPin:
    def test_hal_pin_update_dispatches(self, transport_pair):
        client, server, _ = transport_pair
        seen: list[tuple[str, Any]] = []
        client.set_on_hal_pin_update(lambda n, v: seen.append((n, v)))
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        server.publish_hal_pin("qtcnc.dro_x.value-out", 5.25)
        assert _pump_until(client, lambda: bool(seen))
        assert seen == [("qtcnc.dro_x.value-out", 5.25)]


class TestPublishError:
    def test_error_dispatches_typed(self, transport_pair):
        client, server, _ = transport_pair
        seen: list[ErrorMessage] = []
        client.set_on_error(seen.append)
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        server.publish_error(
            ErrorMessage(
                severity=ErrorSeverity.OPERATOR_ERROR,
                text="tool table missing",
                timestamp=123.456,
            )
        )
        assert _pump_until(client, lambda: bool(seen))
        err = seen[0]
        assert isinstance(err, ErrorMessage)
        assert err.severity == ErrorSeverity.OPERATOR_ERROR
        assert err.text == "tool table missing"
        assert err.timestamp == pytest.approx(123.456)


class TestPublishLifecycle:
    def test_lifecycle_dispatches(self, transport_pair):
        client, server, _ = transport_pair
        seen: list[tuple[str, dict[str, Any]]] = []
        client.set_on_lifecycle(lambda t, p: seen.append((t, p)))
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        server.publish_lifecycle(
            Lifecycle.PROGRAM_LOADED, {"path": "/tmp/part.ngc"},
        )
        assert _pump_until(client, lambda: bool(seen))
        assert seen[0][0] == "program_loaded"
        assert seen[0][1] == {"path": "/tmp/part.ngc"}


class TestPublishSnapshot:
    def test_snapshot_published_as_full_diff(self, transport_pair):
        client, server, handler = transport_pair
        seen: list[list[tuple[str, Any]]] = []
        client.set_on_state_diff(seen.append)
        client.hello()
        time.sleep(_SUB_SETUP_DELAY)

        # Server publishes a full snapshot; client fans it out as a diff
        # containing every StateStore field.
        handler.set_state(replace(handler.state, feed_rate=200.0))
        server.publish_state_snapshot(handler.state)

        assert _pump_until(client, lambda: bool(seen))
        changes = {k: v for k, v in seen[0]}
        assert "feed_rate" in changes
        assert changes["feed_rate"] == pytest.approx(200.0)
        assert "position" in changes  # all fields included


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------


class TestBackgroundSubThread:
    def test_background_thread_drives_pump(self, transport_pair):
        client, server, _ = transport_pair
        seen_lock = threading.Lock()
        seen: list[list[tuple[str, Any]]] = []

        def _on_diff(changes: list[tuple[str, Any]]) -> None:
            with seen_lock:
                seen.append(changes)

        client.set_on_state_diff(_on_diff)
        client.hello()
        client.start_background_sub_thread(poll_ms=25)
        try:
            time.sleep(_SUB_SETUP_DELAY)
            server.publish_state_diff([("feed_rate", 42.0)])
            # Wait for background thread to dispatch.
            assert _wait_for(lambda: bool(seen), timeout=2.0)
            with seen_lock:
                assert seen[0] == [("feed_rate", 42.0)]
        finally:
            client.stop_background_sub_thread()


# ---------------------------------------------------------------------------
# Endpoint split
# ---------------------------------------------------------------------------


class TestEndpointSplit:
    def test_ipc_suffixes(self, transport_pair):
        client, _, _ = transport_pair
        assert client.pub_endpoint.endswith(".pub")
        assert client.cmd_endpoint.endswith(".cmd")

    def test_tcp_port_split(self):
        from qtcnc.transport.zmq_server import _split_endpoint
        pub, cmd = _split_endpoint("tcp://127.0.0.1:5570")
        assert pub == "tcp://127.0.0.1:5570"
        assert cmd == "tcp://127.0.0.1:5571"

    def test_unknown_scheme_raises(self):
        from qtcnc.transport.zmq_server import _split_endpoint
        with pytest.raises(ValueError):
            _split_endpoint("http://example.com")
