"""Tests for qtcnc.core.command — Command facade over Transport."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from qtcnc.core.command import Command, _VERB_TO_PREDICATE, _precheck
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.state import StateStore
from qtcnc.core.status import Status
from qtcnc.core.types import (
    InterpState,
    MachineState,
    MotionMode,
    ProgramState,
    TaskMode,
)
from qtcnc.signals import CommandVerb
from qtcnc.transport.mock import MockTransport


class RecordingTransport(MockTransport):
    """MockTransport that logs every exec_command call for assertion."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[CommandVerb, dict[str, Any]]] = []
        self.loaded: list[str] = []
        self.writes: list[tuple[str, Any]] = []
        self.subscribes: list[str] = []

    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        self.calls.append((verb, kwargs))
        super().exec_command(verb, **kwargs)

    def load_program(self, path: str) -> None:
        self.loaded.append(path)
        super().load_program(path)

    def write_pin(self, name: str, value: Any) -> None:
        self.writes.append((name, value))
        super().write_pin(name, value)

    def subscribe_pin(self, name: str) -> None:
        self.subscribes.append(name)
        super().subscribe_pin(name)


@pytest.fixture
def cmd():
    t = RecordingTransport()
    t.hello()
    return Command(t), t


class TestMachinePower:
    def test_state_estop(self, cmd):
        c, t = cmd
        c.state_estop()
        assert t.calls[-1] == (CommandVerb.STATE_ESTOP, {})

    def test_state_estop_reset(self, cmd):
        c, t = cmd
        c.state_estop_reset()
        assert t.calls[-1] == (CommandVerb.STATE_ESTOP_RESET, {})

    def test_state_on_flow(self, cmd):
        c, t = cmd
        c.state_estop_reset()
        c.state_on()
        assert t.calls[-1] == (CommandVerb.STATE_ON, {})
        assert t.get_snapshot().machine.powered is True

    def test_state_off(self, cmd):
        c, t = cmd
        c.state_estop_reset()
        c.state_on()
        c.state_off()
        assert t.calls[-1] == (CommandVerb.STATE_OFF, {})
        assert t.get_snapshot().machine.powered is False


class TestModeHoming:
    def test_mode(self, cmd):
        c, t = cmd
        c.mode(TaskMode.AUTO)
        assert t.calls[-1] == (CommandVerb.SET_MODE, {"mode": TaskMode.AUTO})

    def test_home(self, cmd):
        c, t = cmd
        c.home(2)
        assert t.calls[-1] == (CommandVerb.HOME, {"joint": 2})

    def test_home_coerces_to_int(self, cmd):
        c, t = cmd
        c.home("1")  # type: ignore[arg-type]
        assert t.calls[-1] == (CommandVerb.HOME, {"joint": 1})

    def test_unhome(self, cmd):
        c, t = cmd
        c.unhome(0)
        assert t.calls[-1] == (CommandVerb.UNHOME, {"joint": 0})

    def test_home_all_joints(self, cmd):
        c, t = cmd
        c.home(-1)
        assert t.calls[-1] == (CommandVerb.HOME, {"joint": -1})


class TestJog:
    def test_jog_continuous(self, cmd):
        c, t = cmd
        c.jog_continuous(axis=1, velocity=10.0)
        assert t.calls[-1] == (CommandVerb.JOG_CONTINUOUS, {"axis": 1, "velocity": 10.0, "joint": -1})

    def test_jog_continuous_with_joint(self, cmd):
        c, t = cmd
        c.jog_continuous(axis=1, velocity=10.0, joint=2)
        assert t.calls[-1] == (CommandVerb.JOG_CONTINUOUS, {"axis": 1, "velocity": 10.0, "joint": 2})

    def test_jog_stop(self, cmd):
        c, t = cmd
        c.jog_stop(1)
        assert t.calls[-1] == (CommandVerb.JOG_STOP, {"axis": 1, "joint": -1})

    def test_jog_stop_with_joint(self, cmd):
        c, t = cmd
        c.jog_stop(1, joint=3)
        assert t.calls[-1] == (CommandVerb.JOG_STOP, {"axis": 1, "joint": 3})

    def test_jog_increment(self, cmd):
        c, t = cmd
        c.jog_increment(axis=2, distance=0.5, velocity=5.0)
        assert t.calls[-1] == (
            CommandVerb.JOG_INCREMENT,
            {"axis": 2, "distance": 0.5, "velocity": 5.0, "joint": -1},
        )

    def test_jog_increment_with_joint(self, cmd):
        c, t = cmd
        c.jog_increment(axis=2, distance=0.5, velocity=5.0, joint=4)
        assert t.calls[-1] == (
            CommandVerb.JOG_INCREMENT,
            {"axis": 2, "distance": 0.5, "velocity": 5.0, "joint": 4},
        )


class TestOverrides:
    def test_feedrate(self, cmd):
        c, t = cmd
        c.feedrate(0.5)
        assert t.calls[-1] == (CommandVerb.FEEDRATE, {"value": 0.5})

    def test_rapidrate(self, cmd):
        c, t = cmd
        c.rapidrate(0.75)
        assert t.calls[-1] == (CommandVerb.RAPIDRATE, {"value": 0.75})

    def test_spindleoverride(self, cmd):
        c, t = cmd
        c.spindleoverride(1.2, index=0)
        assert t.calls[-1] == (
            CommandVerb.SPINDLEOVERRIDE,
            {"index": 0, "value": 1.2},
        )


class TestProgramControl:
    def test_auto_run(self, cmd):
        c, t = cmd
        c.auto_run()
        assert t.calls[-1] == (CommandVerb.AUTO_RUN, {})

    def test_auto_pause(self, cmd):
        c, t = cmd
        c.auto_pause()
        assert t.calls[-1] == (CommandVerb.AUTO_PAUSE, {})

    def test_auto_resume(self, cmd):
        c, t = cmd
        c.auto_resume()
        assert t.calls[-1] == (CommandVerb.AUTO_RESUME, {})

    def test_abort(self, cmd):
        c, t = cmd
        c.abort()
        assert t.calls[-1] == (CommandVerb.ABORT, {})

    def test_auto_step(self, cmd):
        c, t = cmd
        c.auto_step()
        assert t.calls[-1] == (CommandVerb.AUTO_STEP, {})

    def test_mdi(self, cmd):
        c, t = cmd
        c.mdi("G0 X10")
        assert t.calls[-1] == (CommandVerb.MDI, {"command": "G0 X10"})


class TestSpindle:
    def test_spindle_forward(self, cmd):
        c, t = cmd
        c.spindle_forward(1500.0, index=0)
        assert t.calls[-1] == (CommandVerb.SPINDLE_FORWARD, {"index": 0, "speed": 1500.0})

    def test_spindle_reverse(self, cmd):
        c, t = cmd
        c.spindle_reverse(800.0)
        assert t.calls[-1] == (CommandVerb.SPINDLE_REVERSE, {"index": 0, "speed": 800.0})

    def test_spindle_off(self, cmd):
        c, t = cmd
        c.spindle_off()
        assert t.calls[-1] == (CommandVerb.SPINDLE_OFF, {"index": 0})


class TestCoolant:
    def test_mist_on(self, cmd):
        c, t = cmd
        c.mist_on()
        assert t.calls[-1] == (CommandVerb.MIST_ON, {})

    def test_mist_off(self, cmd):
        c, t = cmd
        c.mist_off()
        assert t.calls[-1] == (CommandVerb.MIST_OFF, {})

    def test_flood_on(self, cmd):
        c, t = cmd
        c.flood_on()
        assert t.calls[-1] == (CommandVerb.FLOOD_ON, {})

    def test_flood_off(self, cmd):
        c, t = cmd
        c.flood_off()
        assert t.calls[-1] == (CommandVerb.FLOOD_OFF, {})


class TestFileAndHal:
    def test_load_program(self, cmd):
        c, t = cmd
        c.load_program("/tmp/x.ngc")
        assert t.loaded == ["/tmp/x.ngc"]

    def test_write_pin(self, cmd):
        c, t = cmd
        t.declare_pins([HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT)])
        c.write_pin("qtcnc.a", 3.14)
        assert t.writes == [("qtcnc.a", 3.14)]

    def test_subscribe_pin(self, cmd):
        c, t = cmd
        c.subscribe_pin("halui.machine.is-on")
        assert t.subscribes == ["halui.machine.is-on"]


class TestTrajectoryAndDebug:
    def test_debug(self, cmd):
        c, t = cmd
        c.debug(0x7F)
        assert t.calls[-1] == (CommandVerb.DEBUG, {"mask": 0x7F})

    def test_traj_mode(self, cmd):
        c, t = cmd
        c.traj_mode(MotionMode.TELEOP)
        assert t.calls[-1] == (CommandVerb.TRAJ_MODE, {"mode": MotionMode.TELEOP})

    def test_maxvel(self, cmd):
        c, t = cmd
        c.maxvel(7500.0)
        assert t.calls[-1] == (CommandVerb.MAXVEL, {"value": 7500.0})


class TestToolTable:
    def test_tool_offset_full(self, cmd):
        c, t = cmd
        c.tool_offset(
            3, zoffset=1.5, xoffset=0.1, diameter=6.0,
            frontangle=45.0, backangle=30.0, orientation=6,
        )
        assert t.calls[-1] == (
            CommandVerb.TOOL_OFFSET,
            {
                "tool": 3, "zoffset": 1.5, "xoffset": 0.1,
                "diameter": 6.0, "frontangle": 45.0, "backangle": 30.0,
                "orientation": 6,
            },
        )

    def test_tool_offset_defaults(self, cmd):
        c, t = cmd
        c.tool_offset(2)
        assert t.calls[-1] == (
            CommandVerb.TOOL_OFFSET,
            {
                "tool": 2, "zoffset": 0.0, "xoffset": 0.0,
                "diameter": 0.0, "frontangle": 0.0, "backangle": 0.0,
                "orientation": 0,
            },
        )

    def test_load_tool_table(self, cmd):
        c, t = cmd
        c.load_tool_table()
        assert t.calls[-1] == (CommandVerb.LOAD_TOOL_TABLE, {})


class TestSpindleBrake:
    def test_brake_engage(self, cmd):
        c, t = cmd
        c.brake_engage(index=0)
        assert t.calls[-1] == (CommandVerb.BRAKE_ENGAGE, {"index": 0})

    def test_brake_release(self, cmd):
        c, t = cmd
        c.brake_release()
        assert t.calls[-1] == (CommandVerb.BRAKE_RELEASE, {"index": 0})


class TestInterpreterAndTask:
    def test_task_plan_synch(self, cmd):
        c, t = cmd
        c.task_plan_synch()
        assert t.calls[-1] == (CommandVerb.TASK_PLAN_SYNCH, {})

    def test_override_limits(self, cmd):
        c, t = cmd
        c.override_limits()
        assert t.calls[-1] == (CommandVerb.OVERRIDE_LIMITS, {})

    def test_reset_interpreter(self, cmd):
        c, t = cmd
        c.reset_interpreter()
        assert t.calls[-1] == (CommandVerb.RESET_INTERPRETER, {})

    def test_auto_reverse(self, cmd):
        c, t = cmd
        c.auto_reverse()
        assert t.calls[-1] == (CommandVerb.AUTO_REVERSE, {})

    def test_auto_forward(self, cmd):
        c, t = cmd
        c.auto_forward()
        assert t.calls[-1] == (CommandVerb.AUTO_FORWARD, {})

    def test_set_optional_stop(self, cmd):
        c, t = cmd
        c.set_optional_stop(True)
        assert t.calls[-1] == (CommandVerb.SET_OPTIONAL_STOP, {"enabled": True})

    def test_set_block_delete(self, cmd):
        c, t = cmd
        c.set_block_delete(True)
        assert t.calls[-1] == (CommandVerb.SET_BLOCK_DELETE, {"enabled": True})


class TestJointLimits:
    def test_set_min_limit(self, cmd):
        c, t = cmd
        c.set_min_limit(joint=0, value=-250.0)
        assert t.calls[-1] == (
            CommandVerb.SET_MIN_LIMIT, {"joint": 0, "value": -250.0},
        )

    def test_set_max_limit(self, cmd):
        c, t = cmd
        c.set_max_limit(joint=1, value=250.0)
        assert t.calls[-1] == (
            CommandVerb.SET_MAX_LIMIT, {"joint": 1, "value": 250.0},
        )


class TestOverrideEnables:
    def test_set_feed_override(self, cmd):
        c, t = cmd
        c.set_feed_override(True)
        assert t.calls[-1] == (
            CommandVerb.SET_FEED_OVERRIDE, {"enabled": True},
        )

    def test_set_spindle_override(self, cmd):
        c, t = cmd
        c.set_spindle_override(False, index=0)
        assert t.calls[-1] == (
            CommandVerb.SET_SPINDLE_OVERRIDE,
            {"index": 0, "enabled": False},
        )

    def test_set_feed_hold(self, cmd):
        c, t = cmd
        c.set_feed_hold(True)
        assert t.calls[-1] == (
            CommandVerb.SET_FEED_HOLD, {"enabled": True},
        )

    def test_set_adaptive_feed(self, cmd):
        c, t = cmd
        c.set_adaptive_feed(True)
        assert t.calls[-1] == (
            CommandVerb.SET_ADAPTIVE_FEED, {"enabled": True},
        )


class TestIoOutputs:
    def test_set_digital_output(self, cmd):
        c, t = cmd
        c.set_digital_output(2, True)
        assert t.calls[-1] == (
            CommandVerb.SET_DIGITAL_OUTPUT, {"index": 2, "value": True},
        )

    def test_set_analog_output(self, cmd):
        c, t = cmd
        c.set_analog_output(1, 3.14)
        assert t.calls[-1] == (
            CommandVerb.SET_ANALOG_OUTPUT, {"index": 1, "value": 3.14},
        )


class TestErrorChannelMessages:
    def test_error_msg(self, cmd):
        c, t = cmd
        c.error_msg("broken")
        assert t.calls[-1] == (CommandVerb.ERROR_MSG, {"text": "broken"})

    def test_text_msg(self, cmd):
        c, t = cmd
        c.text_msg("hello")
        assert t.calls[-1] == (CommandVerb.TEXT_MSG, {"text": "hello"})

    def test_display_msg(self, cmd):
        c, t = cmd
        c.display_msg("note")
        assert t.calls[-1] == (CommandVerb.DISPLAY_MSG, {"text": "note"})


class TestSpindleNudges:
    def test_spindle_increase(self, cmd):
        c, t = cmd
        c.spindle_increase()
        assert t.calls[-1] == (CommandVerb.SPINDLE_INCREASE, {"index": 0})

    def test_spindle_decrease(self, cmd):
        c, t = cmd
        c.spindle_decrease(index=1)
        assert t.calls[-1] == (CommandVerb.SPINDLE_DECREASE, {"index": 1})

    def test_spindle_constant(self, cmd):
        c, t = cmd
        c.spindle_constant()
        assert t.calls[-1] == (CommandVerb.SPINDLE_CONSTANT, {"index": 0})


class TestPrecheck:
    """`Command.precheck(verb)` mirrors the daemon's guard table via the
    shared `_VERB_TO_PREDICATE` dict. With no Status wired, precheck
    always returns None; with Status wired, it returns None in the happy
    state and the canonical reason string in refusal states.
    """

    def _ready_state(self, **machine_kwargs: Any) -> StateStore:
        machine = MachineState(
            estop=False,
            powered=True,
            task_mode=TaskMode.MANUAL,
            interp_state=InterpState.IDLE,
            homed=(True, True, True),
            axis_count=3,
            kinematics_identity=True,
        )
        for k, v in machine_kwargs.items():
            machine = replace(machine, **{k: v})
        return StateStore(machine=machine)

    def _make(self, state: StateStore | None = None) -> tuple[Command, Status]:
        t = MockTransport()
        t.hello()
        status = Status(t)
        status.bootstrap()
        if state is not None:
            status._state = state  # swap in the test fixture store
        return Command(t, status=status), status

    def test_no_status_returns_none_for_every_verb(self):
        c = Command(MockTransport())
        for verb in CommandVerb:
            assert c.precheck(verb) is None

    def test_happy_state_permits_jog(self):
        c, _ = self._make(self._ready_state())
        assert c.precheck(CommandVerb.JOG_CONTINUOUS) is None
        assert c.precheck(CommandVerb.JOG_STOP) is None
        assert c.precheck(CommandVerb.JOG_INCREMENT) is None

    def test_jog_refused_when_estopped(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.JOG_CONTINUOUS) == "jog_requires_manual_idle_ready"
        assert c.precheck(CommandVerb.JOG_INCREMENT) == "jog_requires_manual_idle_ready"

    def test_jog_refused_in_auto_mode(self):
        c, _ = self._make(self._ready_state(task_mode=TaskMode.AUTO))
        assert c.precheck(CommandVerb.JOG_CONTINUOUS) == "jog_requires_manual_idle_ready"

    def test_jog_refused_while_interpreter_reading(self):
        c, _ = self._make(self._ready_state(interp_state=InterpState.READING))
        assert c.precheck(CommandVerb.JOG_CONTINUOUS) == "jog_requires_manual_idle_ready"

    def test_jog_stop_allowed_even_when_mode_wrong(self):
        # jog_stop uses is_ready only so an in-flight jog can be
        # stopped cleanly even if the mode was flipped between start
        # and release.
        c, _ = self._make(self._ready_state(task_mode=TaskMode.AUTO))
        assert c.precheck(CommandVerb.JOG_STOP) is None

    def test_state_on_refused_when_estopped(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.STATE_ON) == "state_on_requires_estop_reset"

    def test_state_off_always_allowed(self):
        c, _ = self._make(self._ready_state(estop=True, powered=True))
        # STATE_OFF is not in _VERB_TO_PREDICATE — unconditional permit.
        assert c.precheck(CommandVerb.STATE_OFF) is None

    def test_state_estop_always_allowed(self):
        c, _ = self._make(self._ready_state())
        assert c.precheck(CommandVerb.STATE_ESTOP) is None
        assert c.precheck(CommandVerb.STATE_ESTOP_RESET) is None

    def test_auto_run_refused_without_loaded_program(self):
        state = StateStore(
            machine=MachineState(
                estop=False, powered=True,
                task_mode=TaskMode.AUTO,
                interp_state=InterpState.IDLE,
            ),
        )
        c, _ = self._make(state)
        assert c.precheck(CommandVerb.AUTO_RUN) == "auto_run_requires_auto_idle_loaded"

    def test_auto_run_refused_in_manual_mode(self):
        state = StateStore(
            machine=MachineState(
                estop=False, powered=True,
                task_mode=TaskMode.MANUAL,
                interp_state=InterpState.IDLE,
            ),
            program=ProgramState(path="/tmp/p.ngc", total_lines=1),
        )
        c, _ = self._make(state)
        assert c.precheck(CommandVerb.AUTO_RUN) == "auto_run_requires_auto_idle_loaded"

    def test_auto_run_permitted_with_loaded_program_in_auto_idle(self):
        state = StateStore(
            machine=MachineState(
                estop=False, powered=True,
                task_mode=TaskMode.AUTO,
                interp_state=InterpState.IDLE,
                homed=(True, True, True),
            ),
            program=ProgramState(path="/tmp/p.ngc", total_lines=1),
        )
        c, _ = self._make(state)
        assert c.precheck(CommandVerb.AUTO_RUN) is None

    def test_auto_pause_refused_when_not_running(self):
        state = StateStore(
            machine=MachineState(estop=False, powered=True, task_mode=TaskMode.AUTO),
            program=ProgramState(path="/tmp/p.ngc", is_running=False),
        )
        c, _ = self._make(state)
        assert c.precheck(CommandVerb.AUTO_PAUSE) == "auto_pause_requires_running"

    def test_mdi_refused_when_not_in_mdi_mode(self):
        c, _ = self._make(self._ready_state(task_mode=TaskMode.MANUAL))
        assert c.precheck(CommandVerb.MDI) == "mdi_requires_mdi_idle_ready"

    def test_mdi_permitted_in_mdi_idle_ready(self):
        c, _ = self._make(self._ready_state(task_mode=TaskMode.MDI))
        assert c.precheck(CommandVerb.MDI) is None

    def test_spindle_refused_when_estopped(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.SPINDLE_FORWARD) == "spindle_requires_ready"
        assert c.precheck(CommandVerb.SPINDLE_REVERSE) == "spindle_requires_ready"

    def test_spindle_off_always_allowed(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.SPINDLE_OFF) is None

    def test_home_refused_in_auto_mode(self):
        c, _ = self._make(self._ready_state(task_mode=TaskMode.AUTO))
        assert c.precheck(CommandVerb.HOME) == "home_requires_manual_ready"
        assert c.precheck(CommandVerb.HOME) == "home_requires_manual_ready"

    def test_overrides_always_allowed(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.FEEDRATE) is None
        assert c.precheck(CommandVerb.RAPIDRATE) is None
        assert c.precheck(CommandVerb.SPINDLEOVERRIDE) is None

    def test_coolant_refused_when_estopped(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.MIST_ON) == "coolant_requires_ready"
        assert c.precheck(CommandVerb.FLOOD_ON) == "coolant_requires_ready"

    def test_precheck_free_function_matches_method(self):
        state = self._ready_state(task_mode=TaskMode.AUTO)
        c, _ = self._make(state)
        assert _precheck(state, CommandVerb.JOG_CONTINUOUS) == c.precheck(CommandVerb.JOG_CONTINUOUS)

    def test_traj_mode_refused_when_not_ready(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.TRAJ_MODE) == "traj_mode_requires_ready_idle"

    def test_traj_mode_refused_when_interp_running(self):
        c, _ = self._make(self._ready_state(interp_state=InterpState.READING))
        assert c.precheck(CommandVerb.TRAJ_MODE) == "traj_mode_requires_ready_idle"

    def test_traj_mode_permitted_when_ready_idle(self):
        c, _ = self._make(self._ready_state())
        assert c.precheck(CommandVerb.TRAJ_MODE) is None

    def test_tool_offset_refused_when_not_ready(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.TOOL_OFFSET) == "tool_offset_requires_ready"

    def test_brake_engage_release_refused_when_not_ready(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.BRAKE_ENGAGE) == "brake_requires_ready"
        assert c.precheck(CommandVerb.BRAKE_RELEASE) == "brake_requires_ready"

    def test_load_tool_table_refused_while_program_running(self):
        state = StateStore(
            machine=MachineState(estop=False, powered=True),
            program=ProgramState(path="/tmp/p.ngc", is_running=True),
        )
        c, _ = self._make(state)
        assert c.precheck(CommandVerb.LOAD_TOOL_TABLE) == "load_tool_table_requires_idle"

    def test_load_tool_table_permitted_when_program_idle(self):
        state = StateStore(
            machine=MachineState(estop=False, powered=True),
            program=ProgramState(path="/tmp/p.ngc", is_running=False),
        )
        c, _ = self._make(state)
        assert c.precheck(CommandVerb.LOAD_TOOL_TABLE) is None

    def test_override_limits_refused_outside_manual(self):
        c, _ = self._make(self._ready_state(task_mode=TaskMode.AUTO))
        assert c.precheck(CommandVerb.OVERRIDE_LIMITS) == "override_limits_requires_manual"

    def test_reset_interpreter_refused_when_not_ready(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.RESET_INTERPRETER) == "reset_interpreter_requires_ready"

    def test_auto_direction_refused_outside_auto(self):
        c, _ = self._make(self._ready_state(task_mode=TaskMode.MANUAL))
        assert c.precheck(CommandVerb.AUTO_REVERSE) == "auto_reverse_requires_auto_ready"
        assert c.precheck(CommandVerb.AUTO_FORWARD) == "auto_forward_requires_auto_ready"

    def test_auto_direction_permitted_in_auto_ready(self):
        c, _ = self._make(self._ready_state(task_mode=TaskMode.AUTO))
        assert c.precheck(CommandVerb.AUTO_REVERSE) is None
        assert c.precheck(CommandVerb.AUTO_FORWARD) is None

    def test_set_limits_refused_outside_manual_idle(self):
        c, _ = self._make(self._ready_state(task_mode=TaskMode.AUTO))
        assert c.precheck(CommandVerb.SET_MIN_LIMIT) == "limits_require_manual_idle"
        assert c.precheck(CommandVerb.SET_MAX_LIMIT) == "limits_require_manual_idle"

    def test_set_limits_refused_when_interp_running(self):
        c, _ = self._make(self._ready_state(interp_state=InterpState.READING))
        assert c.precheck(CommandVerb.SET_MIN_LIMIT) == "limits_require_manual_idle"

    def test_spindle_nudges_refused_when_not_ready(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        assert c.precheck(CommandVerb.SPINDLE_INCREASE) == "spindle_requires_ready"
        assert c.precheck(CommandVerb.SPINDLE_DECREASE) == "spindle_requires_ready"
        assert c.precheck(CommandVerb.SPINDLE_CONSTANT) == "spindle_requires_ready"

    def test_unguarded_verbs_always_allowed(self):
        c, _ = self._make(self._ready_state(estop=True, powered=False))
        for v in (
            CommandVerb.DEBUG,
            CommandVerb.MAXVEL,
            CommandVerb.TASK_PLAN_SYNCH,
            CommandVerb.SET_OPTIONAL_STOP,
            CommandVerb.SET_BLOCK_DELETE,
            CommandVerb.SET_FEED_OVERRIDE,
            CommandVerb.SET_SPINDLE_OVERRIDE,
            CommandVerb.SET_FEED_HOLD,
            CommandVerb.SET_ADAPTIVE_FEED,
            CommandVerb.SET_DIGITAL_OUTPUT,
            CommandVerb.SET_ANALOG_OUTPUT,
            CommandVerb.ERROR_MSG,
            CommandVerb.TEXT_MSG,
            CommandVerb.DISPLAY_MSG,
        ):
            assert c.precheck(v) is None, v


class TestToolDbFacade:
    def test_get_tool_db_returns_result(self, cmd):
        c, t = cmd
        result = c.get_tool_db()
        assert hasattr(result, "tools")
        assert hasattr(result, "spindle_tool_id")

    def test_add_tool_forwards_to_transport(self, cmd):
        c, t = cmd
        c.add_tool(5, 5, z_offset=-15.0, diameter=3.0)
        result = c.get_tool_db()
        ids = [e.tool_id for e in result.tools]
        assert 5 in ids

    def test_remove_tool_forwards_to_transport(self, cmd):
        c, t = cmd
        result_before = c.get_tool_db()
        initial_ids = [e.tool_id for e in result_before.tools]
        assert len(initial_ids) > 0
        first_id = initial_ids[0]
        c.remove_tool(first_id)
        result_after = c.get_tool_db()
        assert first_id not in [e.tool_id for e in result_after.tools]

    def test_update_tool_forwards_to_transport(self, cmd):
        c, t = cmd
        result = c.get_tool_db()
        first = result.tools[0]
        c.update_tool(first.tool_id, comment="updated")
        result2 = c.get_tool_db()
        updated = [e for e in result2.tools if e.tool_id == first.tool_id][0]
        assert updated.comment == "updated"
