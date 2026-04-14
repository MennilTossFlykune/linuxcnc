"""Tests for qtcnc.core.command — Command facade over Transport."""

from __future__ import annotations

from typing import Any

import pytest

from qtcnc.core.command import Command
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.types import TaskMode
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
    def test_estop(self, cmd):
        c, t = cmd
        c.estop()
        assert t.calls[-1] == (CommandVerb.ESTOP, {})

    def test_estop_reset(self, cmd):
        c, t = cmd
        c.estop_reset()
        assert t.calls[-1] == (CommandVerb.ESTOP_RESET, {})

    def test_power_on_flow(self, cmd):
        c, t = cmd
        c.estop_reset()
        c.power_on()
        assert t.calls[-1] == (CommandVerb.POWER_ON, {})
        assert t.get_snapshot().machine.powered is True

    def test_power_off(self, cmd):
        c, t = cmd
        c.estop_reset()
        c.power_on()
        c.power_off()
        assert t.calls[-1] == (CommandVerb.POWER_OFF, {})
        assert t.get_snapshot().machine.powered is False


class TestModeHoming:
    def test_set_mode(self, cmd):
        c, t = cmd
        c.set_mode(TaskMode.AUTO)
        assert t.calls[-1] == (CommandVerb.SET_MODE, {"mode": TaskMode.AUTO})

    def test_home_axis(self, cmd):
        c, t = cmd
        c.home_axis(2)
        assert t.calls[-1] == (CommandVerb.HOME_AXIS, {"axis": 2})

    def test_home_axis_coerces_to_int(self, cmd):
        c, t = cmd
        c.home_axis("1")  # type: ignore[arg-type]
        assert t.calls[-1] == (CommandVerb.HOME_AXIS, {"axis": 1})

    def test_unhome_axis(self, cmd):
        c, t = cmd
        c.unhome_axis(0)
        assert t.calls[-1] == (CommandVerb.UNHOME_AXIS, {"axis": 0})

    def test_home_all(self, cmd):
        c, t = cmd
        c.home_all()
        assert t.calls[-1] == (CommandVerb.HOME_ALL, {})


class TestJog:
    def test_jog_start(self, cmd):
        c, t = cmd
        c.jog_start(axis=1, velocity=10.0)
        assert t.calls[-1] == (CommandVerb.JOG_START, {"axis": 1, "velocity": 10.0})

    def test_jog_stop(self, cmd):
        c, t = cmd
        c.jog_stop(1)
        assert t.calls[-1] == (CommandVerb.JOG_STOP, {"axis": 1})

    def test_jog_increment(self, cmd):
        c, t = cmd
        c.jog_increment(axis=2, distance=0.5, velocity=5.0)
        assert t.calls[-1] == (
            CommandVerb.JOG_INCREMENT,
            {"axis": 2, "distance": 0.5, "velocity": 5.0},
        )


class TestOverrides:
    def test_set_feed_override(self, cmd):
        c, t = cmd
        c.set_feed_override(0.5)
        assert t.calls[-1] == (CommandVerb.SET_FEED_OVERRIDE, {"value": 0.5})

    def test_set_rapid_override(self, cmd):
        c, t = cmd
        c.set_rapid_override(0.75)
        assert t.calls[-1] == (CommandVerb.SET_RAPID_OVERRIDE, {"value": 0.75})

    def test_set_spindle_override(self, cmd):
        c, t = cmd
        c.set_spindle_override(1.2, index=0)
        assert t.calls[-1] == (
            CommandVerb.SET_SPINDLE_OVERRIDE,
            {"index": 0, "value": 1.2},
        )


class TestProgramControl:
    def test_program_run(self, cmd):
        c, t = cmd
        c.program_run()
        assert t.calls[-1] == (CommandVerb.PROGRAM_RUN, {})

    def test_program_pause(self, cmd):
        c, t = cmd
        c.program_pause()
        assert t.calls[-1] == (CommandVerb.PROGRAM_PAUSE, {})

    def test_program_resume(self, cmd):
        c, t = cmd
        c.program_resume()
        assert t.calls[-1] == (CommandVerb.PROGRAM_RESUME, {})

    def test_program_stop(self, cmd):
        c, t = cmd
        c.program_stop()
        assert t.calls[-1] == (CommandVerb.PROGRAM_STOP, {})

    def test_program_step(self, cmd):
        c, t = cmd
        c.program_step()
        assert t.calls[-1] == (CommandVerb.PROGRAM_STEP, {})

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

    def test_spindle_stop(self, cmd):
        c, t = cmd
        c.spindle_stop()
        assert t.calls[-1] == (CommandVerb.SPINDLE_STOP, {"index": 0})


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
