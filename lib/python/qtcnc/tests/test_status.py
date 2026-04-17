"""Tests for qtcnc.core.status — Status(QObject) and signal fan-out.

Uses MockTransport as the backend and asserts that signals fire with the
right shape. No QApplication instance is required to emit and observe
signals via direct Python slot connections.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from qtcnc.core.state import StateStore
from qtcnc.core.status import Status
from qtcnc.core.types import (
    ErrorMessage,
    ErrorSeverity,
    InterpState,
    MachineState,
    MotionType,
    Overrides,
    Position,
    ProgramState,
    SpindleDir,
    SpindleState,
    TaskMode,
    TaskState,
    Tool,
)
from qtcnc.signals import CommandVerb, Lifecycle
from qtcnc.transport.mock import MockTransport


class SignalRecorder:
    """Connect a list to every signal on a Status and log emissions."""

    def __init__(self, status: Status) -> None:
        self.events: dict[str, list[tuple]] = {}
        self._track("connected", status.connected, 0)
        self._track("disconnected", status.disconnected, 1)
        self._track("estop_changed", status.estop_changed, 1)
        self._track("power_changed", status.power_changed, 1)
        self._track("task_state_changed", status.task_state_changed, 1)
        self._track("task_mode_changed", status.task_mode_changed, 1)
        self._track("interp_state_changed", status.interp_state_changed, 1)
        self._track("motion_type_changed", status.motion_type_changed, 1)
        self._track("homed_changed", status.homed_changed, 2)
        self._track("all_homed_changed", status.all_homed_changed, 1)
        self._track("homing_changed", status.homing_changed, 2)
        self._track("any_homing_changed", status.any_homing_changed, 1)
        self._track("machine_state_changed", status.machine_state_changed, 1)
        self._track("position_changed", status.position_changed, 1)
        self._track("machine_position_changed", status.machine_position_changed, 1)
        self._track("dtg_changed", status.dtg_changed, 1)
        self._track("axis_position_changed", status.axis_position_changed, 2)
        self._track("program_loading", status.program_loading, 1)
        self._track("program_loaded", status.program_loaded, 2)
        self._track("program_load_failed", status.program_load_failed, 2)
        self._track("program_missing", status.program_missing, 1)
        self._track("program_closed", status.program_closed, 0)
        self._track("program_started", status.program_started, 0)
        self._track("program_paused", status.program_paused, 0)
        self._track("program_finished", status.program_finished, 1)
        self._track("tool_changed", status.tool_changed, 1)
        self._track("tool_in_spindle_changed", status.tool_in_spindle_changed, 1)
        self._track("spindle_speed_changed", status.spindle_speed_changed, 2)
        self._track("spindle_direction_changed", status.spindle_direction_changed, 2)
        self._track("feed_rate_changed", status.feed_rate_changed, 1)
        self._track("rapid_rate_changed", status.rapid_rate_changed, 1)
        self._track("feed_override_changed", status.feed_override_changed, 1)
        self._track("rapid_override_changed", status.rapid_override_changed, 1)
        self._track("spindle_override_changed", status.spindle_override_changed, 2)
        self._track("active_gcodes_changed", status.active_gcodes_changed, 1)
        self._track("active_mcodes_changed", status.active_mcodes_changed, 1)
        self._track("error", status.error, 2)

    def _track(self, name: str, signal, arity: int) -> None:
        bucket: list[tuple] = []
        self.events[name] = bucket
        if arity == 0:
            signal.connect(lambda b=bucket: b.append(()))
        elif arity == 1:
            signal.connect(lambda a, b=bucket: b.append((a,)))
        else:
            signal.connect(lambda a, b, bucket=bucket: bucket.append((a, b)))

    def got(self, name: str) -> list[tuple]:
        return self.events[name]


@pytest.fixture
def setup():
    """Build a Status wired to a MockTransport, optionally pre-connected."""
    def _make(snapshot: StateStore | None = None, pre_connect: bool = True):
        t = MockTransport(initial=snapshot if snapshot is not None else StateStore())
        s = Status(t)
        rec = SignalRecorder(s)
        if pre_connect:
            t.hello()
            s.bootstrap()
            # Clear any signals from bootstrap.
            for bucket in rec.events.values():
                bucket.clear()
        return t, s, rec
    return _make


class TestConnection:
    def test_hello_fires_connected(self):
        t = MockTransport()
        s = Status(t)
        rec = SignalRecorder(s)
        t.hello()
        assert len(rec.got("connected")) == 1

    def test_close_fires_disconnected(self):
        t = MockTransport()
        s = Status(t)
        rec = SignalRecorder(s)
        t.hello()
        t.close()
        assert rec.got("disconnected") == [("closed",)]


class TestBootstrap:
    def test_bootstrap_applies_snapshot(self):
        # Plausible snapshot has machine.estop=True — default store has it too,
        # so expected signals are only for fields that DIFFER from defaults.
        t = MockTransport()
        s = Status(t)
        t.hello()
        s.bootstrap()
        # After bootstrap, state should match the snapshot (except connected,
        # which is already True from on_connected).
        snap = t.get_snapshot()
        assert s.state.task_state == snap.task_state
        assert s.state.active_gcodes == snap.active_gcodes


class TestMachineSignals:
    def test_state_estop_reset_fires_estop_changed(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.STATE_ESTOP_RESET)
        assert rec.got("estop_changed") == [(False,)]
        assert len(rec.got("machine_state_changed")) == 1
        assert len(rec.got("task_state_changed")) == 1

    def test_state_on_fires_power_changed(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.STATE_ESTOP_RESET)
        t.exec_command(CommandVerb.STATE_ON)
        assert rec.got("power_changed") == [(True,)]

    def test_home_fires_per_axis(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.HOME, joint=1)
        assert rec.got("homed_changed") == [(1, True)]

    def test_home_all_fires_all_homed(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.HOME, joint=-1)
        assert rec.got("all_homed_changed") == [(True,)]
        # Each of the three axes emits homed_changed(idx, True).
        assert len(rec.got("homed_changed")) == 3

    def test_set_mode_fires_task_mode_changed(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.SET_MODE, mode=TaskMode.AUTO)
        assert rec.got("task_mode_changed") == [(TaskMode.AUTO,)]

    def test_homing_changed_fires_per_joint(self, setup):
        from qtcnc.core.types import JointState
        snap = StateStore(joints=(JointState(), JointState(), JointState()))
        t, s, rec = setup(snapshot=snap)
        joints = list(s.state.joints)
        joints[1] = replace(joints[1], homing_state=1)
        t.mutate_state(joints=tuple(joints))
        assert rec.got("homing_changed") == [(1, True)]

    def test_any_homing_changed_fires_on_transition(self, setup):
        from qtcnc.core.types import JointState
        snap = StateStore(joints=(JointState(), JointState(), JointState()))
        t, s, rec = setup(snapshot=snap)
        joints = list(s.state.joints)
        joints[0] = replace(joints[0], homing_state=1)
        t.mutate_state(joints=tuple(joints))
        assert rec.got("any_homing_changed") == [(True,)]
        # Finish homing
        joints[0] = replace(joints[0], homing_state=0, homed=True)
        t.mutate_state(joints=tuple(joints))
        assert rec.got("any_homing_changed") == [(True,), (False,)]

    def test_any_homing_not_fired_when_unchanged(self, setup):
        from qtcnc.core.types import JointState
        snap = StateStore(joints=(JointState(), JointState(), JointState()))
        t, s, rec = setup(snapshot=snap)
        joints = list(s.state.joints)
        joints[0] = replace(joints[0], homing_state=1)
        joints[1] = replace(joints[1], homing_state=1)
        t.mutate_state(joints=tuple(joints))
        assert rec.got("any_homing_changed") == [(True,)]
        # One joint finishes but the other is still homing — no transition
        joints[0] = replace(joints[0], homing_state=0, homed=True)
        t.mutate_state(joints=tuple(joints))
        assert rec.got("homing_changed") == [(0, True), (1, True), (0, False)]
        assert rec.got("any_homing_changed") == [(True,)]


class TestPositionSignals:
    def test_position_change_fires_umbrella_and_per_axis(self, setup):
        t, s, rec = setup()
        new_pos = Position(x=1.0, y=2.0, z=3.0)
        t.mutate_state(position=new_pos)
        assert rec.got("position_changed") == [(new_pos,)]
        # axis_position_changed should fire three times (x, y, z)
        axis_events = rec.got("axis_position_changed")
        axes_seen = {a for (a, v) in axis_events}
        assert axes_seen == {0, 1, 2}

    def test_single_axis_change_fires_one_axis(self, setup):
        t, s, rec = setup()
        t.mutate_state(position=Position(x=5.0))
        assert rec.got("axis_position_changed") == [(0, 5.0)]

    def test_machine_position_fires(self, setup):
        t, s, rec = setup()
        new_mpos = Position(x=10.0)
        t.mutate_state(machine_position=new_mpos)
        assert rec.got("machine_position_changed") == [(new_mpos,)]

    def test_dtg_fires(self, setup):
        t, s, rec = setup()
        new_dtg = Position(x=-1.0)
        t.mutate_state(dtg=new_dtg)
        assert rec.got("dtg_changed") == [(new_dtg,)]


class TestFeedRapidOverrideSignals:
    def test_feed_rate(self, setup):
        t, s, rec = setup()
        t.mutate_state(feed_rate=100.0)
        assert rec.got("feed_rate_changed") == [(100.0,)]

    def test_rapid_rate(self, setup):
        t, s, rec = setup()
        t.mutate_state(rapid_rate=500.0)
        assert rec.got("rapid_rate_changed") == [(500.0,)]

    def test_feed_override(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.FEEDRATE, value=0.5)
        assert rec.got("feed_override_changed") == [(0.5,)]

    def test_rapid_override(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.RAPIDRATE, value=0.75)
        assert rec.got("rapid_override_changed") == [(0.75,)]


class TestSpindleSignals:
    def test_spindle_forward_emits_direction_and_speed(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.SPINDLE_FORWARD, speed=1500.0)
        speed_events = rec.got("spindle_speed_changed")
        dir_events = rec.got("spindle_direction_changed")
        assert speed_events == [(0, 1500.0)]
        assert dir_events == [(0, SpindleDir.FORWARD)]

    def test_spindle_override(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.SPINDLEOVERRIDE, index=0, value=1.25)
        assert rec.got("spindle_override_changed") == [(0, 1.25)]


class TestToolSignals:
    def test_tool_in_spindle(self, setup):
        t, s, rec = setup()
        t.mutate_state(tool_in_spindle=5)
        assert rec.got("tool_in_spindle_changed") == [(5,)]

    def test_tool_change_emits_tool_changed(self, setup):
        t, s, rec = setup()
        new_tool = Tool(id=3, pocket=2, diameter=6.0)
        t.mutate_state(tool=new_tool)
        assert rec.got("tool_changed") == [(new_tool,)]


class TestProgramSignals:
    def test_auto_run_fires_program_started(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.AUTO_RUN)
        assert len(rec.got("program_started")) == 1

    def test_auto_pause_fires_program_paused(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.AUTO_RUN)
        t.exec_command(CommandVerb.AUTO_PAUSE)
        assert len(rec.got("program_paused")) == 1

    def test_lifecycle_program_loading(self, setup):
        t, s, rec = setup()
        t.inject_lifecycle(Lifecycle.PROGRAM_LOADING, {"path": "/tmp/f.ngc"})
        assert rec.got("program_loading") == [("/tmp/f.ngc",)]

    def test_lifecycle_program_loaded(self, setup):
        t, s, rec = setup()
        prog = ProgramState(path="/tmp/f.ngc", total_lines=20)
        t.inject_lifecycle(Lifecycle.PROGRAM_LOADED, {"path": "/tmp/f.ngc", "program": prog})
        events = rec.got("program_loaded")
        assert len(events) == 1
        assert events[0] == ("/tmp/f.ngc", prog)

    def test_lifecycle_program_loaded_from_wire_dict(self, setup):
        # Regression: over the wire the daemon's ProgramState is
        # msgpack-serialized via to_wire() into a plain dict, so by the
        # time it reaches `_on_lifecycle` the `program` key is a dict,
        # not a dataclass. Slots on screen handlers expect attribute
        # access (program.total_lines), so the coercion must happen in
        # Status before the signal fires. MockTransport's
        # inject_lifecycle bypasses codec, so we hand-roll the dict form
        # here to mirror what the real ZMQ client hands to Status.
        t, s, rec = setup()
        wire_program = {
            "path": "/tmp/f.ngc",
            "total_lines": 20,
            "current_line": 0,
            "is_running": False,
            "is_paused": False,
        }
        t.inject_lifecycle(
            Lifecycle.PROGRAM_LOADED,
            {"path": "/tmp/f.ngc", "program": wire_program},
        )
        events = rec.got("program_loaded")
        assert len(events) == 1
        path, prog = events[0]
        assert path == "/tmp/f.ngc"
        assert isinstance(prog, ProgramState)
        assert prog.total_lines == 20

    def test_lifecycle_program_load_failed(self, setup):
        t, s, rec = setup()
        t.inject_lifecycle(
            Lifecycle.PROGRAM_LOAD_FAILED,
            {"path": "/tmp/bad.ngc", "reason": "syntax"},
        )
        assert rec.got("program_load_failed") == [("/tmp/bad.ngc", "syntax")]

    def test_lifecycle_program_missing(self, setup):
        t, s, rec = setup()
        t.inject_lifecycle(Lifecycle.PROGRAM_MISSING, {"path": "/tmp/gone.ngc"})
        assert rec.got("program_missing") == [("/tmp/gone.ngc",)]

    def test_lifecycle_program_closed(self, setup):
        t, s, rec = setup()
        t.inject_lifecycle(Lifecycle.PROGRAM_CLOSED, {})
        assert len(rec.got("program_closed")) == 1

    def test_lifecycle_program_finished_success(self, setup):
        t, s, rec = setup()
        t.inject_lifecycle(Lifecycle.PROGRAM_FINISHED, {"success": True})
        assert rec.got("program_finished") == [(True,)]

    def test_lifecycle_program_finished_failure(self, setup):
        t, s, rec = setup()
        t.inject_lifecycle(Lifecycle.PROGRAM_FINISHED, {"success": False})
        assert rec.got("program_finished") == [(False,)]

    def test_state_diff_finish_transition_emits_program_finished(self, setup):
        """is_running True->False with interp_state IDLE should emit a clean
        finish. Exercises the path used by the real daemon when a program
        runs to completion."""
        t, s, rec = setup()
        # Start the program via state_diff (not a command, so we can control
        # interp_state directly).
        t.mutate_state_from_changes([
            ("program", ProgramState(is_running=True)),
            ("machine", replace(s.state.machine, interp_state=InterpState.READING)),
        ])
        assert len(rec.got("program_started")) == 1
        # End with interp_state IDLE.
        t.mutate_state_from_changes([
            ("program", ProgramState(is_running=False)),
            ("machine", replace(s.state.machine, interp_state=InterpState.IDLE)),
        ])
        assert rec.got("program_finished") == [(True,)]

    def test_state_diff_pause_does_not_emit_program_finished(self, setup):
        t, s, rec = setup()
        t.mutate_state_from_changes([
            ("program", ProgramState(is_running=True)),
            ("machine", replace(s.state.machine, interp_state=InterpState.READING)),
        ])
        # Now pause: is_running stays True, is_paused flips True. No finish.
        t.mutate_state_from_changes([
            ("program", ProgramState(is_running=True, is_paused=True)),
            ("machine", replace(s.state.machine, interp_state=InterpState.PAUSED)),
        ])
        assert len(rec.got("program_paused")) == 1
        assert rec.got("program_finished") == []

    def test_resume_does_not_re_emit_program_started(self, setup):
        t, s, rec = setup()
        t.mutate_state_from_changes([
            ("program", ProgramState(is_running=True)),
        ])
        assert len(rec.got("program_started")) == 1
        # Pause.
        t.mutate_state_from_changes([
            ("program", ProgramState(is_running=False, is_paused=True)),
        ])
        assert len(rec.got("program_paused")) == 1
        # Resume — not a fresh start, no new program_started.
        t.mutate_state_from_changes([
            ("program", ProgramState(is_running=True, is_paused=False)),
        ])
        assert len(rec.got("program_started")) == 1

    def test_finish_from_non_idle_reports_failure(self, setup):
        """If the interpreter lands somewhere other than IDLE when the
        program stops, it's an abort, not a clean finish."""
        t, s, rec = setup()
        t.mutate_state_from_changes([
            ("program", ProgramState(is_running=True)),
            ("machine", replace(s.state.machine, interp_state=InterpState.READING)),
        ])
        t.mutate_state_from_changes([
            ("program", ProgramState(is_running=False)),
            ("machine", replace(s.state.machine, interp_state=InterpState.WAITING)),
        ])
        assert rec.got("program_finished") == [(False,)]


class TestActiveCodes:
    def test_active_gcodes(self, setup):
        t, s, rec = setup()
        t.mutate_state(active_gcodes=(20, 90, 17))
        assert rec.got("active_gcodes_changed") == [((20, 90, 17),)]

    def test_active_mcodes(self, setup):
        t, s, rec = setup()
        t.mutate_state(active_mcodes=(3, 8))
        assert rec.got("active_mcodes_changed") == [((3, 8),)]


class TestErrors:
    def test_error_signal(self, setup):
        t, s, rec = setup()
        t.inject_error(ErrorMessage(ErrorSeverity.OPERATOR_ERROR, "boom", 1.0))
        assert rec.got("error") == [(ErrorSeverity.OPERATOR_ERROR, "boom")]


class TestStatePropagation:
    def test_state_mirrors_snapshot(self):
        t = MockTransport()
        s = Status(t)
        t.hello()
        s.bootstrap()
        assert s.state.task_state == t.get_snapshot().task_state
        assert s.state.active_gcodes == t.get_snapshot().active_gcodes

    def test_state_updates_after_exec_command(self, setup):
        t, s, rec = setup()
        t.exec_command(CommandVerb.STATE_ESTOP_RESET)
        assert s.state.machine.estop is False


class TestHandlerCoverage:
    def test_every_statestore_field_has_a_handler(self):
        from dataclasses import fields
        from qtcnc.core.state import StateStore
        from qtcnc.core.status import _FIELD_HANDLERS
        all_fields = {f.name for f in fields(StateStore)}
        assert all_fields == set(_FIELD_HANDLERS.keys())
