"""Daemon contract tests for `qtcnc.server.QtcncServer`.

These tests replace the real `linuxcnc` and `hal` C modules with pure
Python fakes so the daemon can be spun in a thread and driven by a real
`ZmqClientTransport` over an `ipc://` endpoint. No LinuxCNC runtime
required.
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

from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.state import StateStore
from qtcnc.core.types import (
    ErrorMessage,
    ErrorSeverity,
    MachineState,
    Position,
    SpindleDir,
    TaskMode,
    TaskState,
)
from qtcnc.server import (
    QtcncServer,
    execute_command,
    stat_to_state_store,
)
from qtcnc.signals import CommandVerb, MessageType
from qtcnc.transport.base import NackError
from qtcnc.transport.zmq_client import ZmqClientTransport


# ---------------------------------------------------------------------------
# Fake linuxcnc
# ---------------------------------------------------------------------------


class _FakeStat:
    """Writable fake of `linuxcnc.stat()`.

    Tests mutate attributes directly; `poll()` just marks a counter.
    """

    def __init__(self) -> None:
        # Constants-style defaults matching a fresh idle estopped machine.
        self.task_state = 1  # STATE_ESTOP
        self.task_mode = 1  # MODE_MANUAL
        self.interp_state = 1  # INTERP_IDLE
        self.motion_type = 0
        self.actual_position = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        self.joint_actual_position = self.actual_position
        self.position = self.actual_position
        self.dtg = (0.0,) * 9
        self.homed = (0, 0, 0)
        self.feedrate = 1.0
        self.rapidrate = 1.0
        self.spindle = ({
            "speed": 0.0,
            "direction": 0,
            "enabled": False,
            "override": 1.0,
        },)
        self.current_vel = 0.0
        self.max_velocity = 200.0
        self.gcodes = (20, 90, 17, 40, 49, 54, 64, 97)
        self.mcodes = (5, 9)
        self.file = ""
        self.current_line = 0
        self.paused = False
        self.tool_in_spindle = 0
        self.poll_count = 0

    def poll(self) -> None:
        self.poll_count += 1


class _FakeCommand:
    """Writable fake of `linuxcnc.command()`.

    Each method appends a `(name, args)` record to `calls`, and the
    mirror `_FakeLinuxcncModule.stat` is updated to reflect a
    subset of the commanded transitions so tests can verify round-trips
    end-to-end via the state snapshot.
    """

    def __init__(self, module: "_FakeLinuxcncModule") -> None:
        self._module = module
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, tuple(args)))

    def state(self, s: int) -> None:
        self._record("state", s)
        lc = self._module
        stat = self._module.stat_inst
        if s == lc.STATE_ESTOP:
            stat.task_state = lc.STATE_ESTOP
        elif s == lc.STATE_ESTOP_RESET:
            stat.task_state = lc.STATE_ESTOP_RESET
        elif s == lc.STATE_ON:
            stat.task_state = lc.STATE_ON
        elif s == lc.STATE_OFF:
            stat.task_state = lc.STATE_OFF

    def mode(self, m: int) -> None:
        self._record("mode", m)
        self._module.stat_inst.task_mode = m

    def home(self, axis: int) -> None:
        self._record("home", axis)

    def unhome(self, axis: int) -> None:
        self._record("unhome", axis)

    def jog(self, *a: Any) -> None:
        self._record("jog", *a)

    def feedrate(self, value: float) -> None:
        self._record("feedrate", value)
        self._module.stat_inst.feedrate = value

    def rapidrate(self, value: float) -> None:
        self._record("rapidrate", value)
        self._module.stat_inst.rapidrate = value

    def spindleoverride(self, value: float, index: int = 0) -> None:
        self._record("spindleoverride", value, index)

    def auto(self, *a: Any) -> None:
        self._record("auto", *a)

    def mdi(self, command: str) -> None:
        self._record("mdi", command)

    def spindle(self, *a: Any) -> None:
        self._record("spindle", *a)

    def mist(self, v: int) -> None:
        self._record("mist", v)

    def flood(self, v: int) -> None:
        self._record("flood", v)

    def abort(self) -> None:
        self._record("abort")

    def program_open(self, path: str) -> None:
        self._record("program_open", path)
        self._module.stat_inst.file = path

    def wait_complete(self, timeout: float = 1.0) -> int:
        self._record("wait_complete", timeout)
        return 0


class _FakeErrorChannel:
    def __init__(self) -> None:
        self._queue: list[tuple[int, str]] = []

    def poll(self) -> Any:
        if not self._queue:
            return None
        return self._queue.pop(0)

    def push(self, kind: int, text: str) -> None:
        self._queue.append((kind, text))


class _FakeLinuxcncModule:
    """Namespace mimicking the `linuxcnc` C extension module."""

    # State constants
    STATE_ESTOP = 1
    STATE_ESTOP_RESET = 2
    STATE_OFF = 3
    STATE_ON = 4

    # Mode constants
    MODE_MANUAL = 1
    MODE_AUTO = 2
    MODE_MDI = 3

    # Interp constants
    INTERP_IDLE = 1
    INTERP_READING = 2
    INTERP_PAUSED = 3
    INTERP_WAITING = 4

    # Motion types
    MOTION_TYPE_NONE = 0
    MOTION_TYPE_TRAVERSE = 1
    MOTION_TYPE_FEED = 2

    # Spindle
    SPINDLE_FORWARD = 1
    SPINDLE_REVERSE = -1
    SPINDLE_OFF = 0

    # Jog
    JOG_STOP = 0
    JOG_CONTINUOUS = 1
    JOG_INCREMENT = 2

    # Auto
    AUTO_RUN = 0
    AUTO_PAUSE = 1
    AUTO_RESUME = 2
    AUTO_STEP = 3

    # Error channel severities
    OPERATOR_ERROR = 11
    OPERATOR_TEXT = 12
    OPERATOR_DISPLAY = 13
    NML_ERROR = 1

    def __init__(self) -> None:
        self.stat_inst = _FakeStat()
        self.command_inst = _FakeCommand(self)
        self.error_inst = _FakeErrorChannel()

    def stat(self) -> _FakeStat:
        return self.stat_inst

    def command(self) -> _FakeCommand:
        return self.command_inst

    def error_channel(self) -> _FakeErrorChannel:
        return self.error_inst


# ---------------------------------------------------------------------------
# Fake hal
# ---------------------------------------------------------------------------


class _FakePin:
    def __init__(self, name: str, hal_type: int, hal_dir: int) -> None:
        self.name = name
        self.hal_type = hal_type
        self.hal_dir = hal_dir
        self.value: Any = 0

    def set(self, v: Any) -> None:
        self.value = v

    def get(self) -> Any:
        return self.value


class _FakeHalComponent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.pins: dict[str, _FakePin] = {}
        self.ready_called = False
        self.exit_called = False

    def newpin(self, name: str, hal_type: int, hal_dir: int) -> _FakePin:
        if self.ready_called:
            raise RuntimeError(
                f"newpin after ready(): {name}",
            )
        pin = _FakePin(name, hal_type, hal_dir)
        self.pins[name] = pin
        return pin

    def ready(self) -> None:
        self.ready_called = True

    def exit(self) -> None:
        self.exit_called = True


class _FakeHalModule:
    HAL_BIT = 1
    HAL_FLOAT = 2
    HAL_S32 = 3
    HAL_U32 = 4
    HAL_IN = 16
    HAL_OUT = 32
    HAL_IO = 48

    def __init__(self) -> None:
        self.components: dict[str, _FakeHalComponent] = {}
        self.foreign_values: dict[str, Any] = {}

    def component(self, name: str) -> _FakeHalComponent:
        if name not in self.components:
            self.components[name] = _FakeHalComponent(name)
        return self.components[name]

    def get_value(self, name: str) -> Any:
        if name not in self.foreign_values:
            raise KeyError(name)
        return self.foreign_values[name]


# ---------------------------------------------------------------------------
# Server thread helper
# ---------------------------------------------------------------------------


class _ServerThread(threading.Thread):
    def __init__(self, server: QtcncServer) -> None:
        super().__init__(daemon=True, name="test-qtcnc-serverd")
        self._server = server
        self._stop_event = threading.Event()

    def run(self) -> None:
        self._server.start()
        while not self._stop_event.is_set():
            try:
                self._server.serve_once(100)
            except zmq.ZMQError:
                return
            except Exception:
                return

    def shutdown(self) -> None:
        self._stop_event.set()
        self.join(timeout=3.0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def endpoint() -> str:
    base = f"ipc:///tmp/qtcnc-srv-{uuid.uuid4().hex[:12]}"
    yield base
    for suffix in (".pub", ".cmd"):
        path = base[len("ipc://"):] + suffix
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def fakes():
    return _FakeLinuxcncModule(), _FakeHalModule()


@pytest.fixture
def server_client(endpoint: str, fakes):
    lc, halmod = fakes
    server = QtcncServer(
        endpoint,
        linuxcnc_module=lc,
        hal_module=halmod,
        poll_interval_s=0.02,
    )
    server_thread = _ServerThread(server)
    server_thread.start()
    client = ZmqClientTransport(endpoint, request_timeout_ms=2000)
    try:
        yield client, server, lc, halmod
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


def _pump_until(client: ZmqClientTransport, condition, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        client.pump_sub(50)
        if condition():
            return True
    return False


# ---------------------------------------------------------------------------
# Unit: stat_to_state_store
# ---------------------------------------------------------------------------


class TestStatToStateStore:
    def test_idle_estop_defaults(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        store = stat_to_state_store(stat, lc)
        assert store.task_state == TaskState.ESTOP
        assert store.machine.estop is True
        assert store.machine.powered is False
        assert store.position.x == 0.0

    def test_position_mapping(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.actual_position = (1.5, 2.5, 3.5, 10.0, 0, 0, 0, 0, 0)
        store = stat_to_state_store(stat, lc)
        assert store.position.x == pytest.approx(1.5)
        assert store.position.y == pytest.approx(2.5)
        assert store.position.z == pytest.approx(3.5)
        assert store.position.a == pytest.approx(10.0)

    def test_task_state_on(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.task_state = lc.STATE_ON
        store = stat_to_state_store(stat, lc)
        assert store.task_state == TaskState.ON
        assert store.machine.estop is False
        assert store.machine.powered is True

    def test_spindle_mapping(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.spindle = ({
            "speed": 1200.0,
            "direction": 1,
            "enabled": True,
            "override": 0.75,
        },)
        store = stat_to_state_store(stat, lc)
        assert len(store.spindles) == 1
        sp = store.spindles[0]
        assert sp.speed == pytest.approx(1200.0)
        assert sp.direction == SpindleDir.FORWARD
        assert sp.enabled is True
        assert store.overrides.spindles == (pytest.approx(0.75),)

    def test_gcodes_filters_sentinels(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.gcodes = (20, -1, 90, -1, 17)
        store = stat_to_state_store(stat, lc)
        assert store.active_gcodes == (20, 90, 17)

    def test_program_reads_read_line_when_available(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.read_line = 42
        stat.current_line = 7  # should be ignored in favor of read_line
        store = stat_to_state_store(stat, lc)
        assert store.program.current_line == 42

    def test_program_falls_back_to_current_line(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        # Don't set read_line — simulate older linuxcnc builds.
        stat.current_line = 7
        store = stat_to_state_store(stat, lc)
        assert store.program.current_line == 7

    def test_program_is_running_includes_waiting(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.interp_state = lc.INTERP_WAITING
        store = stat_to_state_store(stat, lc)
        assert store.program.is_running is True

    def test_program_is_running_on_reading(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.interp_state = lc.INTERP_READING
        store = stat_to_state_store(stat, lc)
        assert store.program.is_running is True

    def test_program_not_running_on_paused(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.interp_state = lc.INTERP_PAUSED
        store = stat_to_state_store(stat, lc)
        assert store.program.is_running is False
        assert store.program.is_paused is True

    def test_program_is_paused_from_paused_flag(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.paused = True  # task-level paused, interp may still be READING
        stat.interp_state = lc.INTERP_READING
        store = stat_to_state_store(stat, lc)
        assert store.program.is_paused is True


# ---------------------------------------------------------------------------
# Unit: execute_command
# ---------------------------------------------------------------------------


class TestExecuteCommand:
    def test_estop(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(cmd, lc, CommandVerb.ESTOP, {})
        assert cmd.calls[-1] == ("state", (lc.STATE_ESTOP,))

    def test_estop_reset_transitions_stat(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(cmd, lc, CommandVerb.ESTOP_RESET, {})
        assert lc.stat_inst.task_state == lc.STATE_ESTOP_RESET

    def test_set_mode_requires_task_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError):
            execute_command(cmd, lc, CommandVerb.SET_MODE, {"mode": "manual"})

    def test_set_mode_maps_mdi(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(cmd, lc, CommandVerb.SET_MODE, {"mode": TaskMode.MDI})
        assert cmd.calls[-1] == ("mode", (lc.MODE_MDI,))

    def test_program_run_passes_line(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(cmd, lc, CommandVerb.PROGRAM_RUN, {"line": 7})
        assert cmd.calls[-1] == ("auto", (lc.AUTO_RUN, 7))

    def test_spindle_forward(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            cmd, lc, CommandVerb.SPINDLE_FORWARD,
            {"speed": 1500.0, "index": 0},
        )
        assert cmd.calls[-1] == ("spindle", (lc.SPINDLE_FORWARD, 1500.0, 0))

    def test_unknown_verb(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()

        class _Fake(str):
            pass

        with pytest.raises(NackError):
            execute_command(cmd, lc, _Fake("bogus"), {})  # type: ignore


# ---------------------------------------------------------------------------
# End-to-end: client drives daemon over real ZMQ
# ---------------------------------------------------------------------------


class TestDaemonContract:
    def test_hello_and_snapshot(self, server_client):
        client, _, lc, _ = server_client
        reply = client.hello()
        assert reply["daemon"] == "qtcnc-serverd"
        snap = client.get_snapshot()
        assert snap.task_state == TaskState.ESTOP
        assert snap.machine.estop is True

    def test_estop_reset_reflected_in_snapshot(self, server_client):
        client, _, lc, _ = server_client
        client.hello()
        client.exec_command(CommandVerb.ESTOP_RESET)
        # The next stat poll will pick up the fake's updated task_state.
        def ok() -> bool:
            snap = client.get_snapshot()
            return snap.task_state == TaskState.ESTOP_RESET
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not ok():
            time.sleep(0.05)
        assert ok()

    def test_position_changes_flow_via_pubsub(self, server_client):
        client, _, lc, _ = server_client
        seen: list[list[tuple[str, Any]]] = []
        client.set_on_state_diff(seen.append)
        client.hello()
        time.sleep(0.1)  # let SUB subscription propagate
        # Mutate the fake stat; the daemon's poll thread will publish a diff.
        lc.stat_inst.actual_position = (4.25, -1.0, 2.0, 0, 0, 0, 0, 0, 0)

        def saw_position() -> bool:
            for diffs in seen:
                for name, value in diffs:
                    if name == "position" and isinstance(value, Position):
                        if value.x == pytest.approx(4.25):
                            return True
            return False

        assert _pump_until(client, saw_position, timeout_s=2.0)

    def test_declare_pins_creates_hal_pins_and_locks(self, server_client):
        client, _, _, halmod = server_client
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
        comp = halmod.components["qtcnc"]
        assert "dro_x.value-out" in comp.pins
        assert "estop.led" in comp.pins
        assert comp.pins["dro_x.value-out"].hal_type == halmod.HAL_FLOAT
        assert comp.pins["dro_x.value-out"].hal_dir == halmod.HAL_OUT
        assert comp.pins["estop.led"].hal_type == halmod.HAL_BIT
        assert comp.ready_called is True
        # Redeclaring the exact same specs is the inheritance path.
        inherited = client.declare_pins(specs)
        assert inherited.inherited is True
        assert inherited.created == [
            "qtcnc.dro_x.value-out",
            "qtcnc.estop.led",
        ]
        # Declaring a brand new pin after lock is still NACKed.
        with pytest.raises(NackError) as exc:
            client.declare_pins([
                HalPinSpec(name="qtcnc.extra", type=HalType.BIT, dir=HalDir.OUT),
            ])
        assert exc.value.reason == "hal_locked"

    def test_declare_pins_type_mismatch_nacks(self, server_client):
        client, _, _, _ = server_client
        client.hello()
        client.declare_pins([
            HalPinSpec(
                name="qtcnc.dro.value-out",
                type=HalType.FLOAT,
                dir=HalDir.OUT,
            ),
        ])
        with pytest.raises(NackError) as exc:
            client.declare_pins([
                HalPinSpec(
                    name="qtcnc.dro.value-out",
                    type=HalType.BIT,
                    dir=HalDir.OUT,
                ),
            ])
        assert exc.value.reason.startswith("pin_type_mismatch")

    def test_declare_pins_inherits_across_two_clients(self, endpoint: str, fakes):
        """A second real client hitting the same daemon should inherit,
        not be rejected. Exercises the shared-HAL multi-client scenario."""
        from qtcnc.server import QtcncServer as _QtcncServer
        lc, halmod = fakes
        server = _QtcncServer(
            endpoint,
            linuxcnc_module=lc,
            hal_module=halmod,
            poll_interval_s=0.02,
        )
        server_thread = _ServerThread(server)
        server_thread.start()
        client_a = ZmqClientTransport(endpoint, request_timeout_ms=2000)
        client_b = ZmqClientTransport(endpoint, request_timeout_ms=2000)
        try:
            client_a.hello()
            client_b.hello()
            specs = [
                HalPinSpec(
                    name="qtcnc.dro_x.value-out",
                    type=HalType.FLOAT,
                    dir=HalDir.OUT,
                ),
            ]
            a_result = client_a.declare_pins(specs)
            assert a_result.inherited is False
            b_result = client_b.declare_pins(specs)
            assert b_result.inherited is True
            assert b_result.created == ["qtcnc.dro_x.value-out"]
            # HAL pin was only created once.
            comp = halmod.components["qtcnc"]
            assert list(comp.pins.keys()) == ["dro_x.value-out"]
        finally:
            try:
                client_a.close()
            except Exception:
                pass
            try:
                client_b.close()
            except Exception:
                pass
            server_thread.shutdown()
            try:
                server.stop()
            except Exception:
                pass

    def test_write_pin_round_trip(self, server_client):
        client, _, _, halmod = server_client
        client.hello()
        client.declare_pins([
            HalPinSpec(
                name="qtcnc.dro_x.value-out",
                type=HalType.FLOAT,
                dir=HalDir.OUT,
            ),
        ])
        seen: list[tuple[str, Any]] = []
        client.set_on_hal_pin_update(lambda n, v: seen.append((n, v)))
        time.sleep(0.1)  # SUB warm-up
        client.write_pin("qtcnc.dro_x.value-out", 3.75)
        _pump_until(client, lambda: bool(seen))
        assert seen == [("qtcnc.dro_x.value-out", 3.75)]
        # And the underlying fake pin got the value.
        comp = halmod.components["qtcnc"]
        assert comp.pins["dro_x.value-out"].value == 3.75

    def test_write_pin_unknown_nacks(self, server_client):
        client, _, _, _ = server_client
        client.hello()
        with pytest.raises(NackError):
            client.write_pin("qtcnc.ghost", 1.0)

    def test_write_pin_in_direction_nacks(self, server_client):
        client, _, _, _ = server_client
        client.hello()
        client.declare_pins([
            HalPinSpec(name="qtcnc.led", type=HalType.BIT, dir=HalDir.IN),
        ])
        with pytest.raises(NackError) as exc:
            client.write_pin("qtcnc.led", True)
        assert "cannot write IN pin" in exc.value.reason

    def test_error_channel_published(self, server_client):
        client, _, lc, _ = server_client
        seen: list[ErrorMessage] = []
        client.set_on_error(seen.append)
        client.hello()
        time.sleep(0.1)
        lc.error_inst.push(lc.OPERATOR_ERROR, "tool table missing")

        def _saw() -> bool:
            return any(
                e.text == "tool table missing"
                and e.severity == ErrorSeverity.OPERATOR_ERROR
                for e in seen
            )

        assert _pump_until(client, _saw, timeout_s=2.0)

    def test_subscribe_pin_publishes_foreign_updates(self, server_client):
        client, _, _, halmod = server_client
        seen: list[tuple[str, Any]] = []
        client.set_on_hal_pin_update(lambda n, v: seen.append((n, v)))
        client.hello()
        time.sleep(0.1)
        halmod.foreign_values["halui.machine.is-on"] = False
        client.subscribe_pin("halui.machine.is-on")

        def _saw_initial() -> bool:
            return any(n == "halui.machine.is-on" for n, _ in seen)

        assert _pump_until(client, _saw_initial, timeout_s=2.0)
        initial_len = len(seen)

        # Now change the foreign value; daemon should publish a new update.
        halmod.foreign_values["halui.machine.is-on"] = True

        def _saw_true() -> bool:
            return any(
                n == "halui.machine.is-on" and v is True
                for n, v in seen[initial_len:]
            )

        assert _pump_until(client, _saw_true, timeout_s=2.0)

    def test_load_program_sets_file(self, server_client, tmp_path):
        client, _, lc, _ = server_client
        client.hello()
        ngc = tmp_path / "part.ngc"
        ngc.write_text("G0 X0 Y0\nG1 X1\nM2\n")
        client.load_program(str(ngc))
        assert lc.stat_inst.file == str(ngc)

    def test_load_program_publishes_loading_and_loaded_lifecycle(
        self, server_client, tmp_path,
    ):
        client, _, lc, _ = server_client
        seen: list[tuple[str, dict]] = []
        client.set_on_lifecycle(lambda tag, payload: seen.append((tag, payload)))
        client.hello()
        time.sleep(0.1)  # SUB warm-up
        ngc = tmp_path / "prog.ngc"
        ngc.write_text("G0\nG1\nG2\nG3\nM2\n")  # 5 lines
        client.load_program(str(ngc))

        def _saw_loaded() -> bool:
            return any(tag == "program_loaded" for tag, _ in seen)

        assert _pump_until(client, _saw_loaded, timeout_s=2.0)
        tags = [tag for tag, _ in seen]
        assert "program_loading" in tags
        assert "program_loaded" in tags
        loaded_payload = next(p for t, p in seen if t == "program_loaded")
        assert loaded_payload["path"] == str(ngc)
        # The lifecycle payload arrives as a dict since the client doesn't
        # know which payload-type belongs to which tag. Status converts it
        # back into a ProgramState via from_wire before it hits a handler.
        prog = loaded_payload["program"]
        assert isinstance(prog, dict)
        assert prog["path"] == str(ngc)
        assert prog["total_lines"] == 5
        assert prog["is_running"] is False

    def test_load_program_missing_publishes_program_missing(
        self, server_client,
    ):
        client, _, _, _ = server_client
        seen: list[tuple[str, dict]] = []
        client.set_on_lifecycle(lambda tag, payload: seen.append((tag, payload)))
        client.hello()
        time.sleep(0.1)
        with pytest.raises(NackError):
            client.load_program("/tmp/does-not-exist-qtcnc-b1.ngc")

        def _saw_missing() -> bool:
            return any(tag == "program_missing" for tag, _ in seen)

        assert _pump_until(client, _saw_missing, timeout_s=2.0)
        # program_loaded must NOT fire.
        tags = [tag for tag, _ in seen]
        assert "program_loaded" not in tags
        assert "program_missing" in tags


# ---------------------------------------------------------------------------
# POSTGUI ini parsing
# ---------------------------------------------------------------------------


class TestCachedLength:
    def _mk_server(self, fakes, endpoint_str):
        lc, halmod = fakes
        server = QtcncServer(
            endpoint_str,
            linuxcnc_module=lc,
            hal_module=halmod,
            poll_interval_s=0.02,
        )
        return server

    def test_cached_length_empty_path(self, fakes, endpoint):
        server = self._mk_server(fakes, endpoint)
        try:
            assert server._cached_length("") == 0
        finally:
            server.stop()

    def test_cached_length_counts_lines(self, fakes, endpoint, tmp_path):
        server = self._mk_server(fakes, endpoint)
        try:
            f = tmp_path / "x.ngc"
            f.write_text("a\nb\nc\n")
            assert server._cached_length(str(f)) == 3
        finally:
            server.stop()

    def test_cached_length_reuses_cache_until_path_changes(
        self, fakes, endpoint, tmp_path,
    ):
        server = self._mk_server(fakes, endpoint)
        try:
            f = tmp_path / "one.ngc"
            f.write_text("a\nb\n")
            assert server._cached_length(str(f)) == 2
            # Mutate file on disk — should be ignored until path changes.
            f.write_text("a\nb\nc\nd\ne\n")
            assert server._cached_length(str(f)) == 2
            g = tmp_path / "two.ngc"
            g.write_text("x\ny\nz\n")
            assert server._cached_length(str(g)) == 3
        finally:
            server.stop()

    def test_cached_length_missing_file_returns_zero(self, fakes, endpoint):
        server = self._mk_server(fakes, endpoint)
        try:
            assert server._cached_length("/tmp/nope-qtcnc-cached.ngc") == 0
        finally:
            server.stop()


class TestPostguiIni:
    def test_reads_postgui_halfile(self, tmp_path):
        from qtcnc.server import _load_postgui_halfiles
        ini = tmp_path / "test.ini"
        ini.write_text("[HAL]\nPOSTGUI_HALFILE = postgui.hal\n")
        assert _load_postgui_halfiles(str(ini)) == ["postgui.hal"]

    def test_no_ini_returns_empty(self):
        from qtcnc.server import _load_postgui_halfiles
        assert _load_postgui_halfiles(None) == []

    def test_missing_hal_section_returns_empty(self, tmp_path):
        from qtcnc.server import _load_postgui_halfiles
        ini = tmp_path / "minimal.ini"
        ini.write_text("[EMC]\nVERSION = 1.1\n")
        assert _load_postgui_halfiles(str(ini)) == []
