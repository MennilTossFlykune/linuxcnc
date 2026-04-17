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
    InterpState,
    MachineState,
    MotionMode,
    Position,
    ProgramState,
    SpindleDir,
    TaskMode,
    TaskState,
    ToolEntry,
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
        self.motion_mode = 1  # TRAJ_MODE_FREE
        self.kinematics_type = 1  # KINEMATICS_IDENTITY
        self.linear_units = 1.0  # mm (1.0 == units per mm)
        self.axis_mask = 0b111  # XYZ
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
            "brake": False,
            "override_enabled": True,
            "homed": False,
            "orient_state": 0,
            "orient_fault": 0,
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
        self.joints = 3
        self.spindles = 1
        self.num_extrajoints = 0
        self.cycle_time = 0.01
        self.angular_units = 1.0
        self.enabled = False
        self.inpos = True
        self.queue = 0
        self.active_queue = 0
        self.queue_full = False
        self.motion_id = 0
        self.single_stepping = False
        self.velocity = 0.0
        self.acceleration = 0.0
        self.max_acceleration = 0.0
        self.distance_to_go = 0.0
        self.state = 0  # RCS status code
        self.echo_serial_number = 0
        self.exec_state = 2  # DONE
        self.call_level = 0
        self.command = ""
        self.interpreter_errcode = 0
        self.optional_stop = False
        self.block_delete = False
        self.task_paused = 0
        self.input_timeout = False
        self.ini_filename = ""
        self.delay_left = 0.0
        self.queued_mdi_commands = 0
        self.debug = 0
        self.g5x_index = 1
        self.g5x_offset = (0.0,) * 9
        self.g92_offset = (0.0,) * 9
        self.tool_offset = (0.0,) * 9
        self.rotation_xy = 0.0
        self.motion_line = 0
        self.program_units = 2  # MM
        self.settings = (0.0, 0.0, 0.0, 0.0, 0.0)
        self.joint = tuple({
            "jointType": 0, "units": 1.0, "backlash": 0.0,
            "min_position_limit": 0.0, "max_position_limit": 0.0,
            "max_ferror": 0.0, "min_ferror": 0.0,
            "ferror_current": 0.0, "ferror_highmark": 0.0,
            "output": 0.0, "input": 0.0, "velocity": 0.0,
            "inpos": False, "homing": 0, "homed": False,
            "fault": False, "enabled": False,
            "min_soft_limit": False, "max_soft_limit": False,
            "min_hard_limit": False, "max_hard_limit": False,
            "override_limits": False,
        } for _ in range(3))
        self.axis = tuple({
            "velocity": 0.0,
            "min_position_limit": 0.0,
            "max_position_limit": 0.0,
        } for _ in range(9))
        self.tool_table = ()
        self.pocket_prepped = -1
        self.tool_from_pocket = 0
        self.mist = 0
        self.flood = 0
        self.probe_tripped = False
        self.probing = False
        self.probe_val = 0
        self.probed_position = (0.0,) * 9
        self.din = (False,) * 8
        self.dout = (False,) * 8
        self.ain = (0.0,) * 8
        self.aout = (0.0,) * 8
        self.misc_error = (0,) * 10
        self.feed_override_enabled = True
        self.adaptive_feed_enabled = False
        self.feed_hold_enabled = True
        self.heartbeat = 0
        self.taskbeat = 0
        self.estop = True

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

    def teleop_enable(self, v: int) -> None:
        self._record("teleop_enable", v)
        lc = self._module
        stat = self._module.stat_inst
        stat.motion_mode = lc.TRAJ_MODE_TELEOP if v else lc.TRAJ_MODE_FREE

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

    def debug(self, mask: int) -> None:
        self._record("debug", int(mask))

    def traj_mode(self, mode: int) -> None:
        self._record("traj_mode", int(mode))
        self._module.stat_inst.motion_mode = int(mode)

    def maxvel(self, value: float) -> None:
        self._record("maxvel", float(value))

    def tool_offset(
        self, tool: int, zoffset: float, xoffset: float, diameter: float,
        frontangle: float, backangle: float, orientation: int,
    ) -> None:
        self._record(
            "tool_offset", int(tool), float(zoffset), float(xoffset),
            float(diameter), float(frontangle), float(backangle),
            int(orientation),
        )

    def brake(self, engaged: int) -> None:
        self._record("brake", int(engaged))

    def load_tool_table(self) -> None:
        self._record("load_tool_table")

    def task_plan_synch(self) -> None:
        self._record("task_plan_synch")

    def override_limits(self) -> None:
        self._record("override_limits")

    def reset_interpreter(self) -> None:
        self._record("reset_interpreter")

    def set_optional_stop(self, enabled: int) -> None:
        self._record("set_optional_stop", int(enabled))
        self._module.stat_inst.optional_stop = bool(enabled)

    def set_block_delete(self, enabled: int) -> None:
        self._record("set_block_delete", int(enabled))
        self._module.stat_inst.block_delete = bool(enabled)

    def set_min_limit(self, joint: int, value: float) -> None:
        self._record("set_min_limit", int(joint), float(value))

    def set_max_limit(self, joint: int, value: float) -> None:
        self._record("set_max_limit", int(joint), float(value))

    def set_feed_override(self, enabled: int) -> None:
        self._record("set_feed_override", int(enabled))
        self._module.stat_inst.feed_override_enabled = bool(enabled)

    def set_spindle_override(self, enabled: int, index: int = 0) -> None:
        self._record("set_spindle_override", int(enabled), int(index))

    def set_feed_hold(self, enabled: int) -> None:
        self._record("set_feed_hold", int(enabled))
        self._module.stat_inst.feed_hold_enabled = bool(enabled)

    def set_adaptive_feed(self, enabled: int) -> None:
        self._record("set_adaptive_feed", int(enabled))
        self._module.stat_inst.adaptive_feed_enabled = bool(enabled)

    def set_digital_output(self, index: int, value: int) -> None:
        self._record("set_digital_output", int(index), int(value))

    def set_analog_output(self, index: int, value: float) -> None:
        self._record("set_analog_output", int(index), float(value))

    def error_msg(self, text: str) -> None:
        self._record("error_msg", str(text))

    def text_msg(self, text: str) -> None:
        self._record("text_msg", str(text))

    def display_msg(self, text: str) -> None:
        self._record("display_msg", str(text))


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

    # Traj / kinematics
    TRAJ_MODE_FREE = 1
    TRAJ_MODE_COORD = 2
    TRAJ_MODE_TELEOP = 3
    KINEMATICS_IDENTITY = 1
    KINEMATICS_SERIAL = 2
    KINEMATICS_PARALLEL = 3
    KINEMATICS_CUSTOM = 4

    # Auto
    AUTO_RUN = 0
    AUTO_PAUSE = 1
    AUTO_RESUME = 2
    AUTO_STEP = 3
    AUTO_REVERSE = 4
    AUTO_FORWARD = 5

    # Brake
    BRAKE_ENGAGE = 1
    BRAKE_RELEASE = 0

    # Spindle nudge
    SPINDLE_INCREASE = 10
    SPINDLE_DECREASE = 11
    SPINDLE_CONSTANT = 12

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

    def test_linear_units_mm_default(self):
        from qtcnc.core.types import LinearUnits
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.linear_units = 1.0
        store = stat_to_state_store(stat, lc)
        assert store.machine.linear_units == LinearUnits.MM

    def test_linear_units_inch(self):
        from qtcnc.core.types import LinearUnits
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.linear_units = 1.0 / 25.4
        store = stat_to_state_store(stat, lc)
        assert store.machine.linear_units == LinearUnits.INCH

    def test_linear_units_zero_defaults_to_mm(self):
        from qtcnc.core.types import LinearUnits
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.linear_units = 0.0  # INI not loaded yet
        store = stat_to_state_store(stat, lc)
        assert store.machine.linear_units == LinearUnits.MM

    def test_axis_mask_xyz_default(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.axis_mask = 0b111
        store = stat_to_state_store(stat, lc)
        assert store.machine.axis_mask == 0b111
        assert store.machine.axis_letters == ("X", "Y", "Z")

    def test_axis_mask_gantry_four_joints_three_axes(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.axis_mask = 0b111  # X, Y, Z — gantry has four joints but three axes
        stat.joints = 4
        stat.homed = (0, 0, 0, 0)
        store = stat_to_state_store(stat, lc)
        assert store.machine.axis_mask == 0b111
        assert store.machine.axis_letters == ("X", "Y", "Z")
        assert len(store.machine.homed) == 4  # joint count stays accurate

    def test_axis_mask_zero_defaults_to_xyz(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.axis_mask = 0
        store = stat_to_state_store(stat, lc)
        assert store.machine.axis_mask == 0b111

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

    def test_machine_joint_spindle_counts(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.joints = 4
        stat.spindles = 2
        stat.num_extrajoints = 1
        store = stat_to_state_store(stat, lc)
        assert store.machine.joint_count == 4
        assert store.machine.spindle_count == 2
        assert store.machine.num_extrajoints == 1

    def test_machine_cycle_and_units(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.cycle_time = 0.005
        stat.linear_units = 1.0
        stat.angular_units = 1.5
        stat.kinematics_type = lc.KINEMATICS_IDENTITY
        store = stat_to_state_store(stat, lc)
        assert store.machine.cycle_time == pytest.approx(0.005)
        assert store.machine.linear_units_per_mm == pytest.approx(1.0)
        assert store.machine.angular_units_per_deg == pytest.approx(1.5)
        assert store.machine.kinematics_type == lc.KINEMATICS_IDENTITY

    def test_machine_motion_runtime(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.enabled = True
        stat.inpos = True
        stat.queue = 3
        stat.active_queue = 2
        stat.queue_full = False
        stat.motion_id = 42
        stat.single_stepping = True
        stat.velocity = 12.5
        stat.acceleration = 1.25
        stat.max_acceleration = 50.0
        stat.distance_to_go = 7.5
        store = stat_to_state_store(stat, lc)
        assert store.machine.motion_enabled is True
        assert store.machine.inpos is True
        assert store.machine.queue == 3
        assert store.machine.active_queue == 2
        assert store.machine.queue_full is False
        assert store.machine.motion_id == 42
        assert store.machine.single_stepping is True
        assert store.machine.commanded_velocity == pytest.approx(12.5)
        assert store.machine.commanded_acceleration == pytest.approx(1.25)
        assert store.machine.max_acceleration == pytest.approx(50.0)
        assert store.machine.distance_to_go_scalar == pytest.approx(7.5)

    def test_program_motion_line_and_units(self):
        from qtcnc.core.types import ProgramUnits
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.motion_line = 17
        stat.program_units = 1  # INCH
        store = stat_to_state_store(stat, lc)
        assert store.program.motion_line == 17
        assert store.program.program_units == ProgramUnits.INCH

    def test_program_units_cm(self):
        from qtcnc.core.types import ProgramUnits
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.program_units = 3  # CM
        store = stat_to_state_store(stat, lc)
        assert store.program.program_units == ProgramUnits.CM

    def test_program_units_invalid_falls_back_to_mm(self):
        from qtcnc.core.types import ProgramUnits
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.program_units = 99
        store = stat_to_state_store(stat, lc)
        assert store.program.program_units == ProgramUnits.MM

    def test_task_info_defaults(self):
        from qtcnc.core.types import ExecState
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        store = stat_to_state_store(stat, lc)
        assert store.task_info.rcs_state == 0
        assert store.task_info.exec_state == ExecState.DONE
        assert store.task_info.call_level == 0
        assert store.task_info.optional_stop is False
        assert store.task_info.block_delete is False
        assert store.task_info.ini_filename == ""
        assert store.task_info.debug_mask == 0

    def test_task_info_populated(self):
        from qtcnc.core.types import ExecState
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.state = 3
        stat.echo_serial_number = 17
        stat.exec_state = 5  # WAITING_FOR_IO
        stat.call_level = 2
        stat.command = "G1 X10"
        stat.interpreter_errcode = 7
        stat.optional_stop = True
        stat.block_delete = True
        stat.task_paused = 1
        stat.input_timeout = True
        stat.ini_filename = "/tmp/foo.ini"
        stat.delay_left = 0.5
        stat.queued_mdi_commands = 4
        stat.debug = 0x7F
        store = stat_to_state_store(stat, lc)
        assert store.task_info.rcs_state == 3
        assert store.task_info.echo_serial_number == 17
        assert store.task_info.exec_state == ExecState.WAITING_FOR_IO
        assert store.task_info.call_level == 2
        assert store.task_info.active_command_line == "G1 X10"
        assert store.task_info.interpreter_errcode == 7
        assert store.task_info.optional_stop is True
        assert store.task_info.block_delete is True
        assert store.task_info.task_paused == 1
        assert store.task_info.input_timeout is True
        assert store.task_info.ini_filename == "/tmp/foo.ini"
        assert store.task_info.delay_left == pytest.approx(0.5)
        assert store.task_info.queued_mdi_commands == 4
        assert store.task_info.debug_mask == 0x7F

    def test_task_info_unknown_exec_state_falls_back_to_done(self):
        from qtcnc.core.types import ExecState
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.exec_state = 9999
        store = stat_to_state_store(stat, lc)
        assert store.task_info.exec_state == ExecState.DONE

    def test_offsets_defaults(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        store = stat_to_state_store(stat, lc)
        assert store.offsets.g5x_index == 1
        assert store.offsets.g5x.x == 0.0
        assert store.offsets.g5x.y == 0.0
        assert store.offsets.g5x.z == 0.0
        assert store.offsets.g92.x == 0.0
        assert store.offsets.tool_offset.x == 0.0
        assert store.offsets.rotation_xy == 0.0

    def test_offsets_populated(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.g5x_index = 2
        stat.g5x_offset = (1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        stat.g92_offset = (0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        stat.tool_offset = (0.0, 0.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        stat.rotation_xy = 45.0
        store = stat_to_state_store(stat, lc)
        assert store.offsets.g5x_index == 2
        assert store.offsets.g5x.x == pytest.approx(1.0)
        assert store.offsets.g5x.y == pytest.approx(2.0)
        assert store.offsets.g92.x == pytest.approx(0.1)
        assert store.offsets.tool_offset.z == pytest.approx(5.0)
        assert store.offsets.rotation_xy == pytest.approx(45.0)

    def test_active_settings_defaults(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        store = stat_to_state_store(stat, lc)
        assert store.active_settings.sequence_number == 0.0
        assert store.active_settings.feed == 0.0
        assert store.active_settings.speed == 0.0

    def test_active_settings_populated(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.settings = (7.0, 100.0, 2000.0, 0.01, 0.02)
        store = stat_to_state_store(stat, lc)
        assert store.active_settings.sequence_number == pytest.approx(7.0)
        assert store.active_settings.feed == pytest.approx(100.0)
        assert store.active_settings.speed == pytest.approx(2000.0)
        assert store.active_settings.g64_blend_tolerance == pytest.approx(0.01)
        assert store.active_settings.naive_cam_tolerance == pytest.approx(0.02)

    def test_active_settings_short_tuple_falls_back(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.settings = (1.0,)
        store = stat_to_state_store(stat, lc)
        assert store.active_settings.sequence_number == pytest.approx(1.0)
        assert store.active_settings.feed == 0.0
        assert store.active_settings.naive_cam_tolerance == 0.0

    def test_joints_trim_to_active_count(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.joints = 3
        store = stat_to_state_store(stat, lc)
        assert len(store.joints) == 3

    def test_joint_field_mapping(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.joints = 1
        stat.joint = ({
            "jointType": 1, "units": 0.001, "backlash": 0.01,
            "min_position_limit": -100.0, "max_position_limit": 100.0,
            "max_ferror": 0.5, "min_ferror": 0.1,
            "ferror_current": 0.02, "ferror_highmark": 0.05,
            "output": 12.5, "input": 12.4, "velocity": 3.0,
            "inpos": True, "homing": 4, "homed": True,
            "fault": False, "enabled": True,
            "min_soft_limit": False, "max_soft_limit": False,
            "min_hard_limit": False, "max_hard_limit": False,
            "override_limits": False,
        },)
        store = stat_to_state_store(stat, lc)
        j = store.joints[0]
        assert j.joint_type == 1
        assert j.units == pytest.approx(0.001)
        assert j.backlash == pytest.approx(0.01)
        assert j.min_position_limit == pytest.approx(-100.0)
        assert j.max_position_limit == pytest.approx(100.0)
        assert j.ferror_current == pytest.approx(0.02)
        assert j.output == pytest.approx(12.5)
        assert j.input == pytest.approx(12.4)
        assert j.velocity == pytest.approx(3.0)
        assert j.inpos is True
        assert j.homing_state == 4
        assert j.homed is True
        assert j.enabled is True

    def test_joints_fewer_entries_pads_with_defaults(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.joints = 3
        stat.joint = ()  # C module returned nothing
        store = stat_to_state_store(stat, lc)
        assert len(store.joints) == 3
        for j in store.joints:
            assert j.ferror_current == 0.0

    def test_axes_follow_mask(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.axis_mask = 0b111  # XYZ
        stat.axis = tuple({
            "velocity": float(i), "min_position_limit": -10.0 - i,
            "max_position_limit": 10.0 + i,
        } for i in range(9))
        store = stat_to_state_store(stat, lc)
        assert len(store.axes) == 3
        assert store.axes[0].velocity == pytest.approx(0.0)
        assert store.axes[1].velocity == pytest.approx(1.0)
        assert store.axes[2].velocity == pytest.approx(2.0)

    def test_axes_xza_skips_gap(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.axis_mask = (1 << 0) | (1 << 2) | (1 << 3)  # X, Z, A
        stat.axis = tuple({
            "velocity": float(i),
            "min_position_limit": 0.0,
            "max_position_limit": 0.0,
        } for i in range(9))
        store = stat_to_state_store(stat, lc)
        assert len(store.axes) == 3
        assert store.axes[0].velocity == pytest.approx(0.0)  # X
        assert store.axes[1].velocity == pytest.approx(2.0)  # Z
        assert store.axes[2].velocity == pytest.approx(3.0)  # A

    def test_tool_table_empty_default(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        store = stat_to_state_store(stat, lc)
        assert store.tool_table == ()

    def test_tool_table_row_mapping(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.tool_table = (
            (1, 0.0, 0.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 6.0, 45.0, 30.0, 6),
            (2, 0.1, 0.0, 10.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 3.0, 0.0, 0.0, 0),
        )
        store = stat_to_state_store(stat, lc)
        assert len(store.tool_table) == 2
        t1 = store.tool_table[0]
        assert t1.id == 1
        assert t1.offset.z == pytest.approx(5.0)
        assert t1.diameter == pytest.approx(6.0)
        assert t1.frontangle == pytest.approx(45.0)
        assert t1.backangle == pytest.approx(30.0)
        assert t1.orientation == 6
        assert store.tool_table[1].id == 2
        assert store.tool_table[1].diameter == pytest.approx(3.0)

    def test_tool_table_skips_zero_id_rows(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.tool_table = (
            (0,) + (0.0,) * 12 + (0,),
            (1, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0),
        )
        store = stat_to_state_store(stat, lc)
        assert len(store.tool_table) == 1
        assert store.tool_table[0].id == 1

    def test_tool_in_spindle_resolves_from_tool_table(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.tool_in_spindle = 1
        stat.tool_table = (
            (1, 0.0, 0.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 6.0, 45.0, 30.0, 6),
        )
        store = stat_to_state_store(stat, lc)
        assert store.tool.id == 1
        assert store.tool.diameter == pytest.approx(6.0)
        assert store.tool.frontangle == pytest.approx(45.0)
        assert store.tool.orientation == 6

    def test_coolant_defaults_off(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        store = stat_to_state_store(stat, lc)
        assert store.coolant.mist is False
        assert store.coolant.flood is False

    def test_coolant_populated(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.mist = 1
        stat.flood = 1
        store = stat_to_state_store(stat, lc)
        assert store.coolant.mist is True
        assert store.coolant.flood is True

    def test_probe_defaults(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        store = stat_to_state_store(stat, lc)
        assert store.probe.tripped is False
        assert store.probe.probing is False
        assert store.probe.value == 0
        assert store.probe.probed_position.x == 0.0
        assert store.probe.probed_position.y == 0.0
        assert store.probe.probed_position.z == 0.0

    def test_probe_populated(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.probe_tripped = True
        stat.probing = True
        stat.probe_val = 1
        stat.probed_position = (1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        store = stat_to_state_store(stat, lc)
        assert store.probe.tripped is True
        assert store.probe.probing is True
        assert store.probe.value == 1
        assert store.probe.probed_position.x == pytest.approx(1.0)
        assert store.probe.probed_position.y == pytest.approx(2.0)
        assert store.probe.probed_position.z == pytest.approx(3.0)

    def test_io_defaults(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        store = stat_to_state_store(stat, lc)
        assert store.io.digital_in == (False,) * 8
        assert store.io.digital_out == (False,) * 8
        assert store.io.analog_in == (0.0,) * 8
        assert store.io.analog_out == (0.0,) * 8
        assert store.io.misc_error == (0,) * 10
        assert store.io.pocket_prepped == -1
        assert store.io.tool_from_pocket == 0
        assert store.io.aux_estop is True  # starts estopped

    def test_io_populated(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.din = (True, False, True, False)
        stat.dout = (False, True, False, True)
        stat.ain = (1.5, 2.5)
        stat.aout = (3.5,)
        stat.misc_error = (0, 1, 2)
        stat.pocket_prepped = 5
        stat.tool_from_pocket = 2
        stat.estop = False
        store = stat_to_state_store(stat, lc)
        assert store.io.digital_in == (True, False, True, False)
        assert store.io.digital_out == (False, True, False, True)
        assert store.io.analog_in == (pytest.approx(1.5), pytest.approx(2.5))
        assert store.io.analog_out == (pytest.approx(3.5),)
        assert store.io.misc_error == (0, 1, 2)
        assert store.io.pocket_prepped == 5
        assert store.io.tool_from_pocket == 2
        assert store.io.aux_estop is False

    def test_commanded_position_from_position_tuple(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.position = (9.0, 8.0, 7.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        store = stat_to_state_store(stat, lc)
        assert store.commanded_position.x == pytest.approx(9.0)
        assert store.commanded_position.y == pytest.approx(8.0)
        assert store.commanded_position.z == pytest.approx(7.0)

    def test_heartbeats(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.heartbeat = 12345
        stat.taskbeat = 67890
        store = stat_to_state_store(stat, lc)
        assert store.heartbeat == 12345
        assert store.taskbeat == 67890

    def test_overrides_max_velocity_and_flags(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.max_velocity = 250.0
        stat.feed_override_enabled = False
        stat.adaptive_feed_enabled = True
        stat.feed_hold_enabled = False
        store = stat_to_state_store(stat, lc)
        assert store.overrides.max_velocity == pytest.approx(250.0)
        assert store.overrides.feed_enabled is False
        assert store.overrides.adaptive_enabled is True
        assert store.overrides.hold_enabled is False

    def test_spindle_brake_and_homed_fields(self):
        lc = _FakeLinuxcncModule()
        stat = lc.stat()
        stat.spindle = ({
            "speed": 0.0, "direction": 0, "enabled": False, "override": 1.0,
            "brake": True, "override_enabled": False, "homed": True,
            "orient_state": 2, "orient_fault": 7,
        },)
        store = stat_to_state_store(stat, lc)
        sp = store.spindles[0]
        assert sp.brake is True
        assert sp.override_enabled is False
        assert sp.homed is True
        assert sp.orient_state == 2
        assert sp.orient_fault == 7


# ---------------------------------------------------------------------------
# Unit: execute_command
# ---------------------------------------------------------------------------


def _ready_state(
    *,
    task_mode: TaskMode = TaskMode.MANUAL,
    interp_state: InterpState = InterpState.IDLE,
    homed: tuple[bool, ...] = (True, True, True),
    kinematics_identity: bool = True,
    motion_mode: MotionMode = MotionMode.FREE,
    program: ProgramState | None = None,
    coordinates: tuple[str, ...] = ("X", "Y", "Z"),
) -> StateStore:
    """Build a happy-path StateStore for execute_command guard tests."""
    return StateStore(
        task_state=TaskState.ON,
        machine=MachineState(
            estop=False,
            powered=True,
            task_mode=task_mode,
            interp_state=interp_state,
            homed=homed,
            kinematics_identity=kinematics_identity,
            motion_mode=motion_mode,
            coordinates=coordinates,
        ),
        program=program if program is not None else ProgramState(),
    )


class TestExecuteCommand:
    def test_state_estop(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(_ready_state(), cmd, lc, CommandVerb.STATE_ESTOP, {})
        assert cmd.calls[-1] == ("state", (lc.STATE_ESTOP,))

    def test_state_estop_reset_transitions_stat(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            StateStore(), cmd, lc, CommandVerb.STATE_ESTOP_RESET, {},
        )
        assert lc.stat_inst.task_state == lc.STATE_ESTOP_RESET

    def test_set_mode_requires_task_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError):
            execute_command(
                _ready_state(), cmd, lc, CommandVerb.SET_MODE,
                {"mode": "manual"},
            )

    def test_set_mode_maps_mdi(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_MODE,
            {"mode": TaskMode.MDI},
        )
        assert cmd.calls[-1] == ("mode", (lc.MODE_MDI,))

    def test_auto_run_passes_line(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(
                task_mode=TaskMode.AUTO,
                program=ProgramState(path="/p.ngc"),
            ),
            cmd, lc, CommandVerb.AUTO_RUN, {"line": 7},
        )
        assert cmd.calls[-1] == ("auto", (lc.AUTO_RUN, 7))

    def test_spindle_forward(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SPINDLE_FORWARD,
            {"speed": 1500.0, "index": 0},
        )
        assert cmd.calls[-1] == ("spindle", (lc.SPINDLE_FORWARD, 1500.0, 0))

    def test_unknown_verb(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()

        class _Fake(str):
            pass

        with pytest.raises(NackError):
            execute_command(
                _ready_state(), cmd, lc, _Fake("bogus"), {},  # type: ignore
            )

    def test_jog_continuous_identity_homed_uses_axis_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        new_state = execute_command(
            _ready_state(motion_mode=MotionMode.FREE),
            cmd, lc, CommandVerb.JOG_CONTINUOUS,
            {"axis": 1, "velocity": 12.5},
        )
        assert new_state.machine.motion_mode == MotionMode.TELEOP
        teleop = [c for c in cmd.calls if c[0] == "teleop_enable"]
        assert teleop[-1] == ("teleop_enable", (1,))
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 0, 1, 12.5))

    def test_jog_continuous_non_identity_uses_joint_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        new_state = execute_command(
            _ready_state(
                kinematics_identity=False,
                homed=(False, False, False),
                motion_mode=MotionMode.TELEOP,
            ),
            cmd, lc, CommandVerb.JOG_CONTINUOUS,
            {"axis": 2, "velocity": 7.0},
        )
        assert new_state.machine.motion_mode == MotionMode.FREE
        teleop = [c for c in cmd.calls if c[0] == "teleop_enable"]
        assert teleop[-1] == ("teleop_enable", (0,))
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 1, 2, 7.0))

    def test_jog_continuous_identity_not_homed_uses_joint_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(
                kinematics_identity=True,
                homed=(False, False, False),
                motion_mode=MotionMode.FREE,
            ),
            cmd, lc, CommandVerb.JOG_CONTINUOUS,
            {"axis": 0, "velocity": 3.0},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 1, 0, 3.0))

    def test_jog_stop_passes_axis_and_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(motion_mode=MotionMode.TELEOP),
            cmd, lc, CommandVerb.JOG_STOP,
            {"axis": 2},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_STOP, 0, 2))

    def test_jog_increment_passes_distance_and_velocity(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(motion_mode=MotionMode.TELEOP),
            cmd, lc, CommandVerb.JOG_INCREMENT,
            {"axis": 1, "velocity": 6.0, "distance": 0.1},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_INCREMENT, 0, 1, 6.0, 0.1))

    def test_jog_continuous_skips_teleop_toggle_when_already_in_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(motion_mode=MotionMode.TELEOP),
            cmd, lc, CommandVerb.JOG_CONTINUOUS,
            {"axis": 0, "velocity": 5.0},
        )
        teleop = [c for c in cmd.calls if c[0] == "teleop_enable"]
        assert teleop == []
        waits = [c for c in cmd.calls if c[0] == "wait_complete"]
        assert waits == []
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 0, 0, 5.0))

    def test_jog_stop_skips_teleop_toggle_when_already_in_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(motion_mode=MotionMode.TELEOP),
            cmd, lc, CommandVerb.JOG_STOP,
            {"axis": 0},
        )
        teleop = [c for c in cmd.calls if c[0] == "teleop_enable"]
        assert teleop == []


class TestJogJointMapping:
    """Verify that jog commands resolve axis→joint correctly in free mode."""

    def test_jog_free_mode_uses_joint_kwarg(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(
                kinematics_identity=False,
                homed=(False, False, False, False),
                motion_mode=MotionMode.FREE,
                coordinates=("X", "Y", "Y", "Z"),
            ),
            cmd, lc, CommandVerb.JOG_CONTINUOUS,
            {"axis": 1, "velocity": 5.0, "joint": 2},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 1, 2, 5.0))

    def test_jog_free_mode_auto_resolves_axis_to_joint(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(
                kinematics_identity=False,
                homed=(False, False, False, False),
                motion_mode=MotionMode.FREE,
                coordinates=("X", "Y", "Y", "Z"),
            ),
            cmd, lc, CommandVerb.JOG_CONTINUOUS,
            {"axis": 2, "velocity": 5.0},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        # axis 2 = Z, which is joint 3 in XYYZ
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 1, 3, 5.0))

    def test_jog_teleop_mode_ignores_joint_kwarg(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(motion_mode=MotionMode.TELEOP),
            cmd, lc, CommandVerb.JOG_CONTINUOUS,
            {"axis": 1, "velocity": 5.0, "joint": 99},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 0, 1, 5.0))

    def test_jog_stop_free_mode_uses_joint(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(
                kinematics_identity=False,
                homed=(False, False, False, False),
                motion_mode=MotionMode.FREE,
                coordinates=("X", "Y", "Y", "Z"),
            ),
            cmd, lc, CommandVerb.JOG_STOP,
            {"axis": 1, "joint": 2},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_STOP, 1, 2))

    def test_jog_increment_free_mode_uses_joint(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(
                kinematics_identity=False,
                homed=(False, False, False, False),
                motion_mode=MotionMode.FREE,
                coordinates=("X", "Y", "Y", "Z"),
            ),
            cmd, lc, CommandVerb.JOG_INCREMENT,
            {"axis": 2, "velocity": 5.0, "distance": 1.0},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        # axis 2 = Z → joint 3 in XYYZ
        assert jog == ("jog", (lc.JOG_INCREMENT, 1, 3, 5.0, 1.0))

    def test_jog_free_mode_no_coordinates_falls_back_to_identity(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(
                kinematics_identity=False,
                homed=(False, False, False),
                motion_mode=MotionMode.FREE,
                coordinates=(),
            ),
            cmd, lc, CommandVerb.JOG_CONTINUOUS,
            {"axis": 2, "velocity": 5.0},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 1, 2, 5.0))


class TestExecuteCommandGuards:
    def test_state_on_refused_while_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        state = StateStore(machine=MachineState(estop=True, powered=False))
        with pytest.raises(NackError) as exc:
            execute_command(state, cmd, lc, CommandVerb.STATE_ON, {})
        assert exc.value.reason == "state_on_requires_estop_reset"
        assert [c for c in cmd.calls if c[0] == "state"] == []

    def test_state_off_allowed_anywhere(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            StateStore(), cmd, lc, CommandVerb.STATE_OFF, {},
        )
        assert cmd.calls[-1] == ("state", (lc.STATE_OFF,))

    def test_jog_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.JOG_CONTINUOUS,
                {"axis": 0, "velocity": 5.0},
            )
        assert exc.value.reason == "jog_requires_manual_idle_ready"
        assert [c for c in cmd.calls if c[0] == "jog"] == []

    def test_jog_refused_in_auto_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(task_mode=TaskMode.AUTO),
                cmd, lc, CommandVerb.JOG_CONTINUOUS,
                {"axis": 0, "velocity": 5.0},
            )
        assert exc.value.reason == "jog_requires_manual_idle_ready"

    def test_jog_refused_while_interpreter_reading(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(interp_state=InterpState.READING),
                cmd, lc, CommandVerb.JOG_CONTINUOUS,
                {"axis": 0, "velocity": 5.0},
            )
        assert exc.value.reason == "jog_requires_manual_idle_ready"

    def test_jog_allowed_in_happy_path(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        new_state = execute_command(
            _ready_state(),
            cmd, lc, CommandVerb.JOG_CONTINUOUS,
            {"axis": 0, "velocity": 5.0},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"]
        assert jog == [("jog", (lc.JOG_CONTINUOUS, 0, 0, 5.0))]
        assert new_state.machine.motion_mode == MotionMode.TELEOP

    def test_jog_stop_permitted_after_mode_flip(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        # JOG_STOP gates only on is_ready so an in-flight jog can be
        # stopped cleanly even if the mode was flipped out of MANUAL
        # between JOG_START and release.
        execute_command(
            _ready_state(task_mode=TaskMode.AUTO),
            cmd, lc, CommandVerb.JOG_STOP,
            {"axis": 0},
        )
        jog = [c for c in cmd.calls if c[0] == "jog"]
        assert jog == [("jog", (lc.JOG_STOP, 0, 0))]

    def test_jog_stop_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.JOG_STOP,
                {"axis": 0},
            )
        assert exc.value.reason == "jog_stop_requires_ready"

    def test_home_refused_in_auto_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(task_mode=TaskMode.AUTO),
                cmd, lc, CommandVerb.HOME, {"axis": 0},
            )
        assert exc.value.reason == "home_requires_manual_ready"

    def test_home_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.HOME, {"joint": -1},
            )
        assert exc.value.reason == "home_requires_manual_ready"

    def test_home_allowed_even_when_interpreter_reading(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(interp_state=InterpState.READING),
            cmd, lc, CommandVerb.HOME, {"joint": -1},
        )
        assert cmd.calls[-1] == ("home", (-1,))

    def test_auto_run_refused_without_loaded_program(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(task_mode=TaskMode.AUTO),
                cmd, lc, CommandVerb.AUTO_RUN, {"line": 0},
            )
        assert exc.value.reason == "auto_run_requires_auto_idle_loaded"

    def test_auto_run_refused_in_manual_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(program=ProgramState(path="/p.ngc")),
                cmd, lc, CommandVerb.AUTO_RUN, {"line": 0},
            )
        assert exc.value.reason == "auto_run_requires_auto_idle_loaded"

    def test_auto_run_refused_while_running(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(
                    task_mode=TaskMode.AUTO,
                    program=ProgramState(path="/p.ngc", is_running=True),
                ),
                cmd, lc, CommandVerb.AUTO_RUN, {"line": 0},
            )
        assert exc.value.reason == "auto_run_requires_auto_idle_loaded"

    def test_auto_pause_refused_when_not_running(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(
                    task_mode=TaskMode.AUTO,
                    program=ProgramState(path="/p.ngc"),
                ),
                cmd, lc, CommandVerb.AUTO_PAUSE, {},
            )
        assert exc.value.reason == "auto_pause_requires_running"

    def test_auto_resume_refused_when_not_paused(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(
                    task_mode=TaskMode.AUTO,
                    program=ProgramState(path="/p.ngc", is_running=True),
                ),
                cmd, lc, CommandVerb.AUTO_RESUME, {},
            )
        assert exc.value.reason == "auto_resume_requires_paused_auto"

    def test_auto_resume_refused_in_manual(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(
                    task_mode=TaskMode.MANUAL,
                    program=ProgramState(path="/p.ngc", is_paused=True),
                ),
                cmd, lc, CommandVerb.AUTO_RESUME, {},
            )
        assert exc.value.reason == "auto_resume_requires_paused_auto"

    def test_abort_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.ABORT, {},
            )
        assert exc.value.reason == "abort_requires_ready"

    def test_auto_step_refused_without_program_or_pause(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(task_mode=TaskMode.AUTO),
                cmd, lc, CommandVerb.AUTO_STEP, {},
            )
        assert exc.value.reason == "auto_step_requires_auto_ready"

    def test_auto_step_allowed_when_paused(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(
                task_mode=TaskMode.AUTO,
                program=ProgramState(path="/p.ngc", is_paused=True),
            ),
            cmd, lc, CommandVerb.AUTO_STEP, {},
        )
        assert cmd.calls[-1] == ("auto", (lc.AUTO_STEP,))

    def test_mdi_refused_when_not_mdi_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(),
                cmd, lc, CommandVerb.MDI, {"command": "G0 X0"},
            )
        assert exc.value.reason == "mdi_requires_mdi_idle_ready"

    def test_mdi_refused_when_interpreter_reading(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(
                    task_mode=TaskMode.MDI,
                    interp_state=InterpState.READING,
                ),
                cmd, lc, CommandVerb.MDI, {"command": "G0 X0"},
            )
        assert exc.value.reason == "mdi_requires_mdi_idle_ready"

    def test_mdi_allowed_in_happy_path(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(task_mode=TaskMode.MDI),
            cmd, lc, CommandVerb.MDI, {"command": "G0 X1"},
        )
        assert cmd.calls[-1] == ("mdi", ("G0 X1",))

    def test_spindle_forward_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.SPINDLE_FORWARD,
                {"speed": 1000.0, "index": 0},
            )
        assert exc.value.reason == "spindle_requires_ready"

    def test_spindle_off_always_allowed(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            StateStore(), cmd, lc, CommandVerb.SPINDLE_OFF, {"index": 0},
        )
        assert cmd.calls[-1] == ("spindle", (lc.SPINDLE_OFF, 0.0, 0))

    def test_coolant_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.FLOOD_ON, {},
            )
        assert exc.value.reason == "coolant_requires_ready"

    def test_set_mode_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.SET_MODE,
                {"mode": TaskMode.AUTO},
            )
        assert exc.value.reason == "set_mode_requires_ready_idle"

    def test_set_mode_refused_when_interpreter_reading(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(interp_state=InterpState.READING),
                cmd, lc, CommandVerb.SET_MODE,
                {"mode": TaskMode.AUTO},
            )
        assert exc.value.reason == "set_mode_requires_ready_idle"

    def test_auto_switch_mode_on_wrong_mode_raises_not_implemented(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        # auto_switch_mode=True in a body that would call _require_mode
        # raises NotImplementedError. No caller uses it today, so we trip
        # it via a bespoke call through _require_mode indirectly — today
        # execute_command has no verb that invokes _require_mode. The
        # scaffold is proven instead by unit-exercising _require_mode.
        from qtcnc.server import _require_mode
        with pytest.raises(NotImplementedError):
            _require_mode(
                _ready_state(task_mode=TaskMode.AUTO),
                cmd, lc, TaskMode.MANUAL, "x_requires_manual",
                auto_switch=True,
            )

    def test_require_mode_raises_nack_when_auto_switch_off(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        from qtcnc.server import _require_mode
        with pytest.raises(NackError) as exc:
            _require_mode(
                _ready_state(task_mode=TaskMode.AUTO),
                cmd, lc, TaskMode.MANUAL, "x_requires_manual",
                auto_switch=False,
            )
        assert exc.value.reason == "x_requires_manual"

    def test_require_mode_is_noop_when_already_matching(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        from qtcnc.server import _require_mode
        _require_mode(
            _ready_state(task_mode=TaskMode.MANUAL),
            cmd, lc, TaskMode.MANUAL, "x_requires_manual",
            auto_switch=False,
        )
        _require_mode(
            _ready_state(task_mode=TaskMode.MANUAL),
            cmd, lc, TaskMode.MANUAL, "x_requires_manual",
            auto_switch=True,
        )

    def test_set_program_tools_stores_on_state(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        new_state = execute_command(
            _ready_state(task_mode=TaskMode.AUTO),
            cmd, lc, CommandVerb.SET_PROGRAM_TOOLS,
            {"tools": [1, 5, 99]},
        )
        assert new_state.program.requested_tools == frozenset({1, 5, 99})

    def test_set_program_tools_filters_zero(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        new_state = execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_PROGRAM_TOOLS,
            {"tools": [0, 3]},
        )
        assert new_state.program.requested_tools == frozenset({3})

    def test_auto_run_refused_when_tools_missing(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        state = _ready_state(
            task_mode=TaskMode.AUTO,
            program=ProgramState(path="/p.ngc", requested_tools=frozenset({1, 5})),
        )
        state = replace(state, tool_table=(ToolEntry(id=1),))
        with pytest.raises(NackError) as exc:
            execute_command(state, cmd, lc, CommandVerb.AUTO_RUN, {"line": 0})
        assert "auto_run" in exc.value.reason
        assert [c for c in cmd.calls if c[0] == "auto"] == []

    def test_auto_run_allowed_when_all_tools_present(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        state = _ready_state(
            task_mode=TaskMode.AUTO,
            program=ProgramState(path="/p.ngc", requested_tools=frozenset({1, 5})),
        )
        state = replace(state, tool_table=(ToolEntry(id=1), ToolEntry(id=5)))
        execute_command(state, cmd, lc, CommandVerb.AUTO_RUN, {"line": 0})
        assert cmd.calls[-1] == ("auto", (lc.AUTO_RUN, 0))

    def test_auto_run_allowed_when_no_requested_tools(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        state = _ready_state(
            task_mode=TaskMode.AUTO,
            program=ProgramState(path="/p.ngc"),
        )
        execute_command(state, cmd, lc, CommandVerb.AUTO_RUN, {"line": 0})
        assert cmd.calls[-1] == ("auto", (lc.AUTO_RUN, 0))


class TestExecuteCommandNewVerbs:
    """Happy-path coverage of every dispatch branch beyond the core
    estop/power/home/jog/program/spindle/coolant surface.

    Each test verifies the dispatch reaches the corresponding fake
    command method with the expected positional arguments. Guards are
    satisfied via `_ready_state` (and overrides as needed).
    """

    # --- diagnostics / generic config ---

    def test_debug_forwards_mask(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.DEBUG, {"mask": 0x7F},
        )
        assert cmd.calls[-1] == ("debug", (0x7F,))

    def test_error_msg_forwards_text(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.ERROR_MSG,
            {"text": "broken"},
        )
        assert cmd.calls[-1] == ("error_msg", ("broken",))

    def test_text_msg_forwards_text(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.TEXT_MSG,
            {"text": "hello"},
        )
        assert cmd.calls[-1] == ("text_msg", ("hello",))

    def test_display_msg_forwards_text(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.DISPLAY_MSG,
            {"text": "displayed"},
        )
        assert cmd.calls[-1] == ("display_msg", ("displayed",))

    # --- traj / kinematics ---

    def test_traj_mode_teleop(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        new_state = execute_command(
            _ready_state(), cmd, lc, CommandVerb.TRAJ_MODE,
            {"mode": MotionMode.TELEOP},
        )
        assert cmd.calls[-1] == ("traj_mode", (lc.TRAJ_MODE_TELEOP,))
        assert new_state.machine.motion_mode == MotionMode.TELEOP

    def test_traj_mode_coord(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        new_state = execute_command(
            _ready_state(), cmd, lc, CommandVerb.TRAJ_MODE,
            {"mode": MotionMode.COORD},
        )
        assert cmd.calls[-1] == ("traj_mode", (lc.TRAJ_MODE_COORD,))
        assert new_state.machine.motion_mode == MotionMode.COORD

    def test_traj_mode_free(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        new_state = execute_command(
            _ready_state(motion_mode=MotionMode.TELEOP),
            cmd, lc, CommandVerb.TRAJ_MODE,
            {"mode": MotionMode.FREE},
        )
        assert cmd.calls[-1] == ("traj_mode", (lc.TRAJ_MODE_FREE,))
        assert new_state.machine.motion_mode == MotionMode.FREE

    def test_maxvel_forwards_value(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.MAXVEL,
            {"value": 250.0},
        )
        assert cmd.calls[-1] == ("maxvel", (250.0,))

    # --- tool table / offsets ---

    def test_tool_offset_forwards_all_args(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.TOOL_OFFSET,
            {
                "tool": 3, "zoffset": 1.5, "xoffset": 0.25,
                "diameter": 6.0, "frontangle": 45.0, "backangle": 30.0,
                "orientation": 6,
            },
        )
        assert cmd.calls[-1] == (
            "tool_offset", (3, 1.5, 0.25, 6.0, 45.0, 30.0, 6),
        )

    def test_load_tool_table(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.LOAD_TOOL_TABLE, {},
        )
        assert cmd.calls[-1] == ("load_tool_table", ())

    # --- spindle brake ---

    def test_brake_engage(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.BRAKE_ENGAGE, {},
        )
        assert cmd.calls[-1] == ("brake", (1,))

    def test_brake_release(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.BRAKE_RELEASE, {},
        )
        assert cmd.calls[-1] == ("brake", (0,))

    # --- task / interpreter control ---

    def test_task_plan_synch(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.TASK_PLAN_SYNCH, {},
        )
        assert cmd.calls[-1] == ("task_plan_synch", ())

    def test_override_limits(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.OVERRIDE_LIMITS, {},
        )
        assert cmd.calls[-1] == ("override_limits", ())

    def test_reset_interpreter(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.RESET_INTERPRETER, {},
        )
        assert cmd.calls[-1] == ("reset_interpreter", ())

    def test_auto_reverse(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(task_mode=TaskMode.AUTO),
            cmd, lc, CommandVerb.AUTO_REVERSE, {},
        )
        assert cmd.calls[-1] == ("auto", (4,))

    def test_auto_forward(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(task_mode=TaskMode.AUTO),
            cmd, lc, CommandVerb.AUTO_FORWARD, {},
        )
        assert cmd.calls[-1] == ("auto", (5,))

    # --- task config toggles ---

    def test_set_optional_stop_on(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_OPTIONAL_STOP,
            {"enabled": True},
        )
        assert cmd.calls[-1] == ("set_optional_stop", (1,))

    def test_set_optional_stop_off(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_OPTIONAL_STOP,
            {"enabled": False},
        )
        assert cmd.calls[-1] == ("set_optional_stop", (0,))

    def test_set_block_delete_on(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_BLOCK_DELETE,
            {"enabled": True},
        )
        assert cmd.calls[-1] == ("set_block_delete", (1,))

    # --- joint limits ---

    def test_set_min_limit_forwards_joint_and_value(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_MIN_LIMIT,
            {"joint": 1, "value": -100.5},
        )
        assert cmd.calls[-1] == ("set_min_limit", (1, -100.5))

    def test_set_max_limit_forwards_joint_and_value(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_MAX_LIMIT,
            {"joint": 2, "value": 250.0},
        )
        assert cmd.calls[-1] == ("set_max_limit", (2, 250.0))

    # --- override-enable toggles ---

    def test_set_feed_override(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_FEED_OVERRIDE,
            {"enabled": False},
        )
        assert cmd.calls[-1] == ("set_feed_override", (0,))

    def test_set_spindle_override(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_SPINDLE_OVERRIDE,
            {"enabled": True, "index": 0},
        )
        assert cmd.calls[-1] == ("set_spindle_override", (1, 0))

    def test_set_feed_hold(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_FEED_HOLD,
            {"enabled": False},
        )
        assert cmd.calls[-1] == ("set_feed_hold", (0,))

    def test_set_adaptive_feed(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_ADAPTIVE_FEED,
            {"enabled": True},
        )
        assert cmd.calls[-1] == ("set_adaptive_feed", (1,))

    # --- digital / analog I/O ---

    def test_set_digital_output_high(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_DIGITAL_OUTPUT,
            {"index": 3, "value": True},
        )
        assert cmd.calls[-1] == ("set_digital_output", (3, 1))

    def test_set_digital_output_low(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_DIGITAL_OUTPUT,
            {"index": 0, "value": False},
        )
        assert cmd.calls[-1] == ("set_digital_output", (0, 0))

    def test_set_analog_output_forwards_value(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SET_ANALOG_OUTPUT,
            {"index": 2, "value": 3.5},
        )
        assert cmd.calls[-1] == ("set_analog_output", (2, 3.5))

    # --- spindle increment / decrement / constant ---

    def test_spindle_increase_default_index(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SPINDLE_INCREASE, {},
        )
        assert cmd.calls[-1] == ("spindle", (10, 0))

    def test_spindle_decrease(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SPINDLE_DECREASE,
            {"index": 1},
        )
        assert cmd.calls[-1] == ("spindle", (11, 1))

    def test_spindle_constant(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        execute_command(
            _ready_state(), cmd, lc, CommandVerb.SPINDLE_CONSTANT,
            {"index": 0},
        )
        assert cmd.calls[-1] == ("spindle", (12, 0))

    # --- guard negatives: 14 verbs with explicit preconditions ---

    def test_traj_mode_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.TRAJ_MODE,
                {"mode": MotionMode.TELEOP},
            )
        assert exc.value.reason == "traj_mode_requires_ready_idle"
        assert [c for c in cmd.calls if c[0] == "traj_mode"] == []

    def test_traj_mode_refused_when_interpreter_reading(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(interp_state=InterpState.READING),
                cmd, lc, CommandVerb.TRAJ_MODE,
                {"mode": MotionMode.TELEOP},
            )
        assert exc.value.reason == "traj_mode_requires_ready_idle"

    def test_traj_mode_invalid_mode_type(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(), cmd, lc, CommandVerb.TRAJ_MODE,
                {"mode": 1},
            )
        assert "traj_mode requires MotionMode" in exc.value.reason

    def test_tool_offset_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.TOOL_OFFSET,
                {"tool": 1},
            )
        assert exc.value.reason == "tool_offset_requires_ready"

    def test_brake_engage_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.BRAKE_ENGAGE, {},
            )
        assert exc.value.reason == "brake_requires_ready"

    def test_brake_release_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.BRAKE_RELEASE, {},
            )
        assert exc.value.reason == "brake_requires_ready"

    def test_load_tool_table_refused_while_running(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(
                    program=ProgramState(path="/p.ngc", is_running=True),
                ),
                cmd, lc, CommandVerb.LOAD_TOOL_TABLE, {},
            )
        assert exc.value.reason == "load_tool_table_requires_idle"

    def test_override_limits_refused_in_auto_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(task_mode=TaskMode.AUTO),
                cmd, lc, CommandVerb.OVERRIDE_LIMITS, {},
            )
        assert exc.value.reason == "override_limits_requires_manual"

    def test_reset_interpreter_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.RESET_INTERPRETER, {},
            )
        assert exc.value.reason == "reset_interpreter_requires_ready"

    def test_auto_reverse_refused_in_manual(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(),
                cmd, lc, CommandVerb.AUTO_REVERSE, {},
            )
        assert exc.value.reason == "auto_reverse_requires_auto_ready"

    def test_auto_forward_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.AUTO_FORWARD, {},
            )
        assert exc.value.reason == "auto_forward_requires_auto_ready"

    def test_set_min_limit_refused_in_auto_mode(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(task_mode=TaskMode.AUTO),
                cmd, lc, CommandVerb.SET_MIN_LIMIT,
                {"joint": 0, "value": -10.0},
            )
        assert exc.value.reason == "limits_require_manual_idle"

    def test_set_max_limit_refused_when_interpreter_reading(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                _ready_state(interp_state=InterpState.READING),
                cmd, lc, CommandVerb.SET_MAX_LIMIT,
                {"joint": 0, "value": 10.0},
            )
        assert exc.value.reason == "limits_require_manual_idle"

    def test_spindle_increase_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.SPINDLE_INCREASE, {},
            )
        assert exc.value.reason == "spindle_requires_ready"

    def test_spindle_decrease_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.SPINDLE_DECREASE, {},
            )
        assert exc.value.reason == "spindle_requires_ready"

    def test_spindle_constant_refused_when_estopped(self):
        lc = _FakeLinuxcncModule()
        cmd = lc.command()
        with pytest.raises(NackError) as exc:
            execute_command(
                StateStore(), cmd, lc, CommandVerb.SPINDLE_CONSTANT, {},
            )
        assert exc.value.reason == "spindle_requires_ready"


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

    def test_state_estop_reset_reflected_in_snapshot(self, server_client):
        client, _, lc, _ = server_client
        client.hello()
        client.exec_command(CommandVerb.STATE_ESTOP_RESET)
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

    def _warm_up_ready(self, client, lc) -> None:
        """Drive the fake stat into a ready-to-jog state and wait for the
        daemon's poll loop to propagate it to the snapshot."""
        lc.stat_inst.task_state = lc.STATE_ON

        def _ready() -> bool:
            snap = client.get_snapshot()
            return snap.machine.is_ready

        assert _pump_until(client, _ready, timeout_s=2.0)

    def test_jog_continuous_uses_all_homed_from_polled_state(self, server_client):
        client, server, lc, _ = server_client
        client.hello()
        self._warm_up_ready(client, lc)
        lc.stat_inst.homed = (1, 1, 1)

        def _homed() -> bool:
            snap = client.get_snapshot()
            return snap.machine.is_all_homed

        assert _pump_until(client, _homed, timeout_s=2.0)
        client.exec_command(CommandVerb.JOG_CONTINUOUS, axis=1, velocity=8.5)
        jog = [c for c in lc.command_inst.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 0, 1, 8.5))
        teleop = [c for c in lc.command_inst.calls if c[0] == "teleop_enable"][-1]
        assert teleop == ("teleop_enable", (1,))

    def test_jog_continuous_not_homed_falls_back_to_joint_mode(self, server_client):
        client, server, lc, _ = server_client
        client.hello()
        self._warm_up_ready(client, lc)
        # Fake stat starts at TRAJ_MODE_FREE so no teleop toggle is needed
        # for a joint-mode jog; assert that jjogmode=1 is dispatched.
        client.exec_command(CommandVerb.JOG_CONTINUOUS, axis=2, velocity=4.0)
        jog = [c for c in lc.command_inst.calls if c[0] == "jog"][-1]
        assert jog == ("jog", (lc.JOG_CONTINUOUS, 1, 2, 4.0))

    def test_repeated_jog_commands_dont_re_toggle_teleop(self, server_client):
        client, server, lc, _ = server_client
        client.hello()
        self._warm_up_ready(client, lc)
        lc.stat_inst.homed = (1, 1, 1)

        def _homed() -> bool:
            snap = client.get_snapshot()
            return snap.machine.is_all_homed

        assert _pump_until(client, _homed, timeout_s=2.0)
        # First jog toggles into teleop mode.
        client.exec_command(CommandVerb.JOG_CONTINUOUS, axis=0, velocity=5.0)
        client.exec_command(CommandVerb.JOG_STOP, axis=0)
        client.exec_command(CommandVerb.JOG_CONTINUOUS, axis=1, velocity=5.0)
        client.exec_command(CommandVerb.JOG_STOP, axis=1)
        client.exec_command(CommandVerb.JOG_CONTINUOUS, axis=2, velocity=5.0)
        client.exec_command(CommandVerb.JOG_STOP, axis=2)
        teleop_calls = [c for c in lc.command_inst.calls if c[0] == "teleop_enable"]
        assert len(teleop_calls) == 1
        assert teleop_calls[0] == ("teleop_enable", (1,))

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


# ---------------------------------------------------------------------------
# Tool DB integration tests
# ---------------------------------------------------------------------------


class TestToolDb:
    """Verify the daemon's on_get_tool_db / on_add_tool / on_remove_tool /
    on_update_tool handlers against a real sqlite file."""

    def _mk_server(self, fakes, endpoint_str, tool_db_path, random_tc=False):
        lc, halmod = fakes
        return QtcncServer(
            endpoint_str,
            linuxcnc_module=lc,
            hal_module=halmod,
            poll_interval_s=0.02,
            tool_db_path=tool_db_path,
            random_toolchanger=random_tc,
        )

    def _seed_tools(self, db_path):
        from qtcnc.tools.tooldb_schema import open_db, upsert_tool
        conn = open_db(db_path)
        upsert_tool(conn, 1, pocket=1, z_offset=-25.4, diameter=6.0,
                     comment="6mm endmill")
        upsert_tool(conn, 2, pocket=2, z_offset=-30.0, diameter=10.0,
                     comment="10mm endmill")
        conn.close()

    def test_get_tool_db_returns_seeded_tools(self, fakes, endpoint, tmp_path):
        db_path = str(tmp_path / "tools.db")
        self._seed_tools(db_path)
        server = self._mk_server(fakes, endpoint, db_path)
        try:
            result = server.on_get_tool_db()
            tools = result["tools"]
            assert len(tools) == 2
            assert tools[0].tool_id == 1
            assert tools[0].diameter == 6.0
            assert tools[0].comment == "6mm endmill"
            assert tools[1].tool_id == 2
            assert tools[1].diameter == 10.0
        finally:
            server.stop()

    def test_get_tool_db_no_db_raises_nack(self, fakes, endpoint):
        server = self._mk_server(fakes, endpoint, tool_db_path=None)
        try:
            with pytest.raises(NackError, match="no_tool_db"):
                server.on_get_tool_db()
        finally:
            server.stop()

    def test_add_tool_creates_and_calls_load(self, fakes, endpoint, tmp_path):
        db_path = str(tmp_path / "tools.db")
        self._seed_tools(db_path)
        server = self._mk_server(fakes, endpoint, db_path)
        lc, _ = fakes
        try:
            server.on_add_tool(5, 5, {"z_offset": -15.0, "diameter": 3.0})
            result = server.on_get_tool_db()
            ids = [t.tool_id for t in result["tools"]]
            assert 5 in ids
            t5 = [t for t in result["tools"] if t.tool_id == 5][0]
            assert t5.pocket == 5
            assert t5.z_offset == -15.0
            assert t5.diameter == 3.0
            assert any(c[0] == "load_tool_table" for c in lc.command_inst.calls)
        finally:
            server.stop()

    def test_remove_tool_deletes_and_calls_load(self, fakes, endpoint, tmp_path):
        db_path = str(tmp_path / "tools.db")
        self._seed_tools(db_path)
        server = self._mk_server(fakes, endpoint, db_path)
        lc, _ = fakes
        try:
            server.on_remove_tool(1)
            result = server.on_get_tool_db()
            ids = [t.tool_id for t in result["tools"]]
            assert 1 not in ids
            assert 2 in ids
            assert any(c[0] == "load_tool_table" for c in lc.command_inst.calls)
        finally:
            server.stop()

    def test_update_tool_modifies_and_calls_load(self, fakes, endpoint, tmp_path):
        db_path = str(tmp_path / "tools.db")
        self._seed_tools(db_path)
        server = self._mk_server(fakes, endpoint, db_path)
        lc, _ = fakes
        try:
            server.on_update_tool(1, {"z_offset": -99.9, "comment": "modified"})
            result = server.on_get_tool_db()
            t1 = [t for t in result["tools"] if t.tool_id == 1][0]
            assert t1.z_offset == -99.9
            assert t1.comment == "modified"
            assert t1.diameter == 6.0
            assert any(c[0] == "load_tool_table" for c in lc.command_inst.calls)
        finally:
            server.stop()

    def test_add_tool_no_db_raises_nack(self, fakes, endpoint):
        server = self._mk_server(fakes, endpoint, tool_db_path=None)
        try:
            with pytest.raises(NackError, match="no_tool_db"):
                server.on_add_tool(1, 1, {})
        finally:
            server.stop()

    def test_remove_tool_no_db_raises_nack(self, fakes, endpoint):
        server = self._mk_server(fakes, endpoint, tool_db_path=None)
        try:
            with pytest.raises(NackError, match="no_tool_db"):
                server.on_remove_tool(1)
        finally:
            server.stop()

    def test_update_tool_no_db_raises_nack(self, fakes, endpoint):
        server = self._mk_server(fakes, endpoint, tool_db_path=None)
        try:
            with pytest.raises(NackError, match="no_tool_db"):
                server.on_update_tool(1, {})
        finally:
            server.stop()

    def test_random_toolchanger_flag_propagates(self, fakes, endpoint, tmp_path):
        db_path = str(tmp_path / "tools.db")
        self._seed_tools(db_path)
        server = self._mk_server(fakes, endpoint, db_path, random_tc=True)
        try:
            result = server.on_get_tool_db()
            assert result["random_toolchanger"] is True
        finally:
            server.stop()

    def test_spindle_state_returns_zero_by_default(self, fakes, endpoint, tmp_path):
        db_path = str(tmp_path / "tools.db")
        self._seed_tools(db_path)
        server = self._mk_server(fakes, endpoint, db_path)
        try:
            result = server.on_get_tool_db()
            assert result["spindle_tool_id"] == 0
        finally:
            server.stop()
