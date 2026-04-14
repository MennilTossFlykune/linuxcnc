"""Tests for qtcnc.transport.mock — MockTransport end-to-end behavior."""

from __future__ import annotations

from typing import Any

import pytest

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
from qtcnc.signals import CommandVerb
from qtcnc.transport.base import NackError, TransportClosed
from qtcnc.transport.mock import MockTransport, _plausible_snapshot


class Recorder:
    """Collects every callback the transport fires for assertion."""

    def __init__(self, t: MockTransport) -> None:
        self.state_diffs: list[list[tuple[str, Any]]] = []
        self.pin_updates: list[tuple[str, Any]] = []
        self.errors: list[ErrorMessage] = []
        self.lifecycle: list[tuple[str, dict]] = []
        self.connected_count = 0
        self.disconnected: list[str] = []
        t.set_on_state_diff(self.state_diffs.append)
        t.set_on_hal_pin_update(lambda n, v: self.pin_updates.append((n, v)))
        t.set_on_error(self.errors.append)
        t.set_on_lifecycle(lambda tag, p: self.lifecycle.append((tag, p)))
        t.set_on_connected(self._on_connected)
        t.set_on_disconnected(lambda reason: self.disconnected.append(reason))

    def _on_connected(self) -> None:
        self.connected_count += 1


class TestSnapshot:
    def test_default_snapshot_is_plausible(self):
        t = MockTransport()
        s = t.get_snapshot()
        assert s.connected is True
        assert s.task_state == TaskState.ESTOP
        assert s.machine.estop is True
        assert s.machine.powered is False
        assert s.position == Position()

    def test_custom_initial_state(self):
        initial = StateStore(feed_rate=42.0)
        t = MockTransport(initial=initial)
        assert t.get_snapshot().feed_rate == 42.0


class TestHello:
    def test_hello_returns_protocol_version(self):
        t = MockTransport()
        reply = t.hello()
        assert "protocol_version" in reply

    def test_hello_fires_connected(self):
        t = MockTransport()
        rec = Recorder(t)
        t.hello()
        assert rec.connected_count == 1

    def test_is_connected_after_hello(self):
        t = MockTransport()
        assert t.is_connected is False
        t.hello()
        assert t.is_connected is True


class TestCloseAndClosed:
    def test_close_fires_disconnected(self):
        t = MockTransport()
        rec = Recorder(t)
        t.hello()
        t.close()
        assert rec.disconnected == ["closed"]

    def test_close_is_idempotent(self):
        t = MockTransport()
        rec = Recorder(t)
        t.close()
        t.close()
        assert rec.disconnected == ["closed"]

    def test_operations_after_close_raise(self):
        t = MockTransport()
        t.close()
        with pytest.raises(TransportClosed):
            t.hello()
        with pytest.raises(TransportClosed):
            t.get_snapshot()
        with pytest.raises(TransportClosed):
            t.exec_command(CommandVerb.ESTOP)


class TestExecCommandEstopPower:
    def test_estop_reset_releases_estop(self):
        t = MockTransport()
        rec = Recorder(t)
        t.exec_command(CommandVerb.ESTOP_RESET)
        assert t.get_snapshot().machine.estop is False
        assert t.get_snapshot().task_state == TaskState.ESTOP_RESET
        # state_diff should have been emitted
        assert len(rec.state_diffs) == 1

    def test_power_on_requires_estop_released(self):
        t = MockTransport()
        with pytest.raises(NackError):
            t.exec_command(CommandVerb.POWER_ON)

    def test_power_on_after_estop_reset(self):
        t = MockTransport()
        t.exec_command(CommandVerb.ESTOP_RESET)
        t.exec_command(CommandVerb.POWER_ON)
        s = t.get_snapshot()
        assert s.machine.powered is True
        assert s.task_state == TaskState.ON

    def test_estop_while_powered_clears_power(self):
        t = MockTransport()
        t.exec_command(CommandVerb.ESTOP_RESET)
        t.exec_command(CommandVerb.POWER_ON)
        t.exec_command(CommandVerb.ESTOP)
        s = t.get_snapshot()
        assert s.machine.estop is True
        assert s.machine.powered is False


class TestExecCommandOther:
    def test_set_mode(self):
        t = MockTransport()
        t.exec_command(CommandVerb.SET_MODE, mode=TaskMode.AUTO)
        assert t.get_snapshot().machine.task_mode == TaskMode.AUTO

    def test_set_mode_requires_task_mode_enum(self):
        t = MockTransport()
        with pytest.raises(NackError):
            t.exec_command(CommandVerb.SET_MODE, mode="auto")

    def test_home_axis(self):
        t = MockTransport()
        t.exec_command(CommandVerb.HOME_AXIS, axis=1)
        homed = t.get_snapshot().machine.homed
        assert homed[1] is True
        assert homed[0] is False

    def test_unhome_axis(self):
        t = MockTransport()
        t.exec_command(CommandVerb.HOME_ALL)
        t.exec_command(CommandVerb.UNHOME_AXIS, axis=2)
        homed = t.get_snapshot().machine.homed
        assert homed[0] is True
        assert homed[2] is False

    def test_home_all(self):
        t = MockTransport()
        t.exec_command(CommandVerb.HOME_ALL)
        assert all(t.get_snapshot().machine.homed)

    def test_feed_override(self):
        t = MockTransport()
        t.exec_command(CommandVerb.SET_FEED_OVERRIDE, value=0.5)
        assert t.get_snapshot().overrides.feed == 0.5

    def test_spindle_override_grows_tuple(self):
        t = MockTransport()
        t.exec_command(CommandVerb.SET_SPINDLE_OVERRIDE, index=2, value=1.5)
        sp = t.get_snapshot().overrides.spindles
        assert len(sp) >= 3
        assert sp[2] == 1.5

    def test_spindle_forward(self):
        t = MockTransport()
        t.exec_command(CommandVerb.SPINDLE_FORWARD, index=0, speed=1200.0)
        sp = t.get_snapshot().spindles[0]
        assert sp.direction == SpindleDir.FORWARD
        assert sp.speed == 1200.0
        assert sp.enabled is True

    def test_spindle_stop(self):
        t = MockTransport()
        t.exec_command(CommandVerb.SPINDLE_FORWARD, speed=1000)
        t.exec_command(CommandVerb.SPINDLE_STOP)
        sp = t.get_snapshot().spindles[0]
        assert sp.direction == SpindleDir.STOP
        assert sp.enabled is False
        assert sp.speed == 0.0

    def test_program_run_pause_resume_stop(self):
        t = MockTransport()
        t.exec_command(CommandVerb.PROGRAM_RUN)
        assert t.get_snapshot().program.is_running is True
        t.exec_command(CommandVerb.PROGRAM_PAUSE)
        assert t.get_snapshot().program.is_paused is True
        t.exec_command(CommandVerb.PROGRAM_RESUME)
        assert t.get_snapshot().program.is_paused is False
        t.exec_command(CommandVerb.PROGRAM_STOP)
        assert t.get_snapshot().program.is_running is False

    def test_noop_verbs_dont_mutate(self):
        t = MockTransport()
        before = t.get_snapshot()
        for verb in (
            CommandVerb.JOG_START, CommandVerb.JOG_STOP,
            CommandVerb.MDI, CommandVerb.MIST_ON, CommandVerb.FLOOD_ON,
        ):
            t.exec_command(verb)
        assert t.get_snapshot() == before


class TestLoadProgram:
    def test_load_program_fires_lifecycle(self):
        t = MockTransport()
        rec = Recorder(t)
        t.load_program("/tmp/test.ngc")
        tags = [tag for tag, _ in rec.lifecycle]
        assert "program_loading" in tags
        assert "program_loaded" in tags

    def test_load_program_updates_state(self):
        t = MockTransport()
        t.load_program("/tmp/test.ngc")
        assert t.get_snapshot().program.path == "/tmp/test.ngc"


class TestDeclarePins:
    def test_declare_pins_creates(self):
        t = MockTransport()
        specs = [
            HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT),
            HalPinSpec(name="qtcnc.b", type=HalType.BIT, dir=HalDir.IN),
        ]
        result = t.declare_pins(specs)
        assert result.created == ["qtcnc.a", "qtcnc.b"]
        assert result.inherited is False
        assert t.pin_value("qtcnc.a") == 0.0
        assert t.pin_value("qtcnc.b") is False

    def test_declare_pins_uses_initial(self):
        t = MockTransport()
        t.declare_pins([
            HalPinSpec(name="qtcnc.c", type=HalType.FLOAT, dir=HalDir.OUT, initial=7.5),
        ])
        assert t.pin_value("qtcnc.c") == 7.5

    def test_second_declare_new_pin_nacks(self):
        t = MockTransport()
        t.declare_pins([HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT)])
        with pytest.raises(NackError) as exc:
            t.declare_pins([HalPinSpec(name="qtcnc.b", type=HalType.BIT, dir=HalDir.IN)])
        assert exc.value.reason == "hal_locked"

    def test_second_declare_matching_inherits(self):
        t = MockTransport()
        specs = [
            HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT),
            HalPinSpec(name="qtcnc.b", type=HalType.BIT, dir=HalDir.IN),
        ]
        first = t.declare_pins(specs)
        assert first.inherited is False
        second = t.declare_pins(specs)
        assert second.inherited is True
        assert second.created == ["qtcnc.a", "qtcnc.b"]

    def test_second_declare_type_mismatch_nacks(self):
        t = MockTransport()
        t.declare_pins([HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT)])
        with pytest.raises(NackError) as exc:
            t.declare_pins([HalPinSpec(name="qtcnc.a", type=HalType.BIT, dir=HalDir.OUT)])
        assert exc.value.reason.startswith("pin_type_mismatch")

    def test_duplicate_in_single_batch(self):
        t = MockTransport()
        specs = [
            HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT),
            HalPinSpec(name="qtcnc.a", type=HalType.BIT, dir=HalDir.OUT),
        ]
        with pytest.raises(NackError):
            t.declare_pins(specs)

    def test_s32_default(self):
        t = MockTransport()
        t.declare_pins([HalPinSpec(name="qtcnc.n", type=HalType.S32, dir=HalDir.OUT)])
        assert t.pin_value("qtcnc.n") == 0


class TestWritePin:
    def test_write_dispatches(self):
        t = MockTransport()
        rec = Recorder(t)
        t.declare_pins([HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT)])
        t.write_pin("qtcnc.a", 3.14)
        assert rec.pin_updates == [("qtcnc.a", 3.14)]
        assert t.pin_value("qtcnc.a") == 3.14

    def test_write_unknown_pin_raises(self):
        t = MockTransport()
        with pytest.raises(NackError):
            t.write_pin("nope", 1)

    def test_write_in_pin_raises(self):
        t = MockTransport()
        t.declare_pins([HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.IN)])
        with pytest.raises(NackError):
            t.write_pin("qtcnc.a", 1.0)


class TestSubscribePin:
    def test_subscribe_stores(self):
        t = MockTransport()
        t.subscribe_pin("halui.machine.is-on")
        assert "halui.machine.is-on" in t._subscribed


class TestTestMutators:
    def test_mutate_state_fires_diff(self):
        t = MockTransport()
        rec = Recorder(t)
        t.mutate_state(feed_rate=100.0)
        assert len(rec.state_diffs) == 1
        assert rec.state_diffs[0] == [("feed_rate", 100.0)]

    def test_mutate_state_from_changes(self):
        t = MockTransport()
        rec = Recorder(t)
        t.mutate_state_from_changes([("feed_rate", 50.0), ("rapid_rate", 200.0)])
        assert len(rec.state_diffs) == 1
        assert ("feed_rate", 50.0) in rec.state_diffs[0]
        assert ("rapid_rate", 200.0) in rec.state_diffs[0]

    def test_mutate_state_no_change_no_diff(self):
        t = MockTransport()
        rec = Recorder(t)
        t.mutate_state(feed_rate=t.get_snapshot().feed_rate)
        assert rec.state_diffs == []

    def test_mutate_pin_fires_update(self):
        t = MockTransport()
        rec = Recorder(t)
        t.mutate_pin("halui.machine.is-on", True)
        assert rec.pin_updates == [("halui.machine.is-on", True)]

    def test_inject_error(self):
        t = MockTransport()
        rec = Recorder(t)
        err = ErrorMessage(severity=ErrorSeverity.OPERATOR_ERROR, text="boom", timestamp=1.0)
        t.inject_error(err)
        assert rec.errors == [err]

    def test_inject_lifecycle(self):
        t = MockTransport()
        rec = Recorder(t)
        t.inject_lifecycle("daemon_ready", {})
        assert rec.lifecycle == [("daemon_ready", {})]


class TestDiffCorrectness:
    def test_single_estop_reset_emits_two_fields(self):
        t = MockTransport()
        rec = Recorder(t)
        t.exec_command(CommandVerb.ESTOP_RESET)
        # machine + task_state should both change
        assert len(rec.state_diffs) == 1
        names = {name for name, _ in rec.state_diffs[0]}
        assert "machine" in names
        assert "task_state" in names
