"""End-to-end tests for the minimal-screen widgets in mock mode."""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import Any

import pytest

from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.core.command import Command
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.status import Status
from qtcnc.core.types import (
    InterpState,
    MachineState,
    MotionMode,
    MotionType,
    Offsets,
    Position,
    SpindleDir,
    SpindleState,
    TaskMode,
    TaskState,
)
from qtcnc.signals import CommandVerb
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.base import QtcncWidget, collect_pin_declarations
from qtcnc.widgets.common.action_button import ActionButton
from qtcnc.widgets.common.state_label import StateKind, StateLabel
from qtcnc.widgets.hal import HalPinHub


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _make_world(*widget_factories):
    """Build a QMainWindow with a MockTransport, Status, Command, HalPinHub,
    and the requested widgets. Runs the simplified bootstrap sequence:
    hello, snapshot, collect+declare pins, wire signals.
    """
    t = MockTransport()
    window = QMainWindow()
    widgets: list[QtcncWidget] = [f(window) for f in widget_factories]
    status = Status(t)
    hub = HalPinHub(t)
    cmd = Command(t, status=status)
    window.qtcnc_status = status
    window.qtcnc_command = cmd
    window.qtcnc_hal = hub
    t.hello()
    status.bootstrap()
    specs = collect_pin_declarations(widgets)
    if specs:
        result = t.declare_pins(specs)
        for name in result.created:
            hub.get_or_create(name)
    for w in widgets:
        w.qtcnc_setup()
    return t, status, cmd, window, widgets


def _state_label(name: str, state: str, **props) -> Any:
    def factory(window):
        lbl = StateLabel(window)
        lbl.setObjectName(name)
        lbl._set_state(state)
        for key, value in props.items():
            getattr(lbl, f"_set_{key}")(value)
        return lbl
    return factory


# ---------------------------------------------------------------------------
# StateLabel — task_state (default)
# ---------------------------------------------------------------------------


class TestStateLabelTaskState:
    def _mk(self):
        return _make_world(_state_label("state_lbl", "task_state"))

    def test_state_property_is_enum(self):
        t, status, cmd, win, (lbl,) = self._mk()
        assert lbl.state == StateKind.task_state
        assert lbl.state.name == "task_state"

    def test_state_accepts_enum_instance(self):
        lbl = StateLabel()
        lbl._set_state(StateKind.feed_rate)
        assert lbl.state is StateKind.feed_rate

    def test_initial_is_estopped(self):
        t, status, cmd, win, (lbl,) = self._mk()
        assert lbl.text() == "ESTOPPED"

    def test_updates_on_state_change(self):
        t, status, cmd, win, (lbl,) = self._mk()
        t.exec_command(CommandVerb.STATE_ESTOP_RESET)
        QApplication.processEvents()
        assert lbl.text() == "ESTOP RESET"
        t.exec_command(CommandVerb.STATE_ON)
        QApplication.processEvents()
        assert lbl.text() == "ON"


# ---------------------------------------------------------------------------
# StateLabel — task_mode / interp_state / motion_type / motion_mode
# ---------------------------------------------------------------------------


class TestStateLabelMachineEnums:
    def test_task_mode_initial_manual(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("m", "task_mode"))
        assert lbl.text() == "MANUAL"

    def test_task_mode_follows_set_mode(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("m", "task_mode"))
        t.exec_command(CommandVerb.STATE_ESTOP_RESET)
        t.exec_command(CommandVerb.STATE_ON)
        t.exec_command(CommandVerb.SET_MODE, mode=TaskMode.AUTO)
        QApplication.processEvents()
        assert lbl.text() == "AUTO"

    def test_interp_state_initial_idle(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("i", "interp_state"))
        assert lbl.text() == "IDLE"

    def test_interp_state_follows_state_diff(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("i", "interp_state"))
        old = t.get_snapshot().machine
        t.mutate_state(machine=replace(old, interp_state=InterpState.READING))
        QApplication.processEvents()
        assert lbl.text() == "READING"

    def test_motion_type_follows_machine_state(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("mt", "motion_type"))
        assert lbl.text() == "NONE"
        old = t.get_snapshot().machine
        t.mutate_state(machine=replace(old, motion_type=MotionType.FEED))
        QApplication.processEvents()
        assert lbl.text() == "FEED"

    def test_motion_mode_follows_machine_state(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("mm", "motion_mode"))
        assert lbl.text() == "FREE"
        old = t.get_snapshot().machine
        t.mutate_state(machine=replace(old, motion_mode=MotionMode.TELEOP))
        QApplication.processEvents()
        assert lbl.text() == "TELEOP"

    def test_linear_units_shows_mm_by_default(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("lu", "linear_units"))
        assert lbl.text() == "MM"


# ---------------------------------------------------------------------------
# StateLabel — g5x_index
# ---------------------------------------------------------------------------


class TestStateLabelG5xIndex:
    def test_initial_is_g54(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("g5", "g5x_index"))
        assert lbl.text() == "G54"

    def test_follows_offsets_change(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("g5", "g5x_index"))
        old_off = t.get_snapshot().offsets
        t.mutate_state(offsets=replace(old_off, g5x_index=3))
        QApplication.processEvents()
        assert lbl.text() == "G56"


# ---------------------------------------------------------------------------
# StateLabel — DRO variants (work / machine / dtg)
# ---------------------------------------------------------------------------


class TestStateLabelDro:
    def test_dro_work_initial_zero(self):
        t, status, cmd, win, (lbl,) = _make_world(
            _state_label("dro_x", "dro_work", axis=0),
        )
        assert lbl.text().startswith("X:")
        assert "0.000" in lbl.text()

    def test_dro_work_follows_position(self):
        t, status, cmd, win, (lbl,) = _make_world(
            _state_label("dro_x", "dro_work", axis=0),
        )
        t.mutate_state(position=Position(x=1.234))
        QApplication.processEvents()
        assert "1.234" in lbl.text()
        assert lbl.text().startswith("X:")

    def test_dro_work_axis_y_is_independent(self):
        t, status, cmd, win, (lbl,) = _make_world(
            _state_label("dro_y", "dro_work", axis=1),
        )
        t.mutate_state(position=Position(x=9.9, y=3.140))
        QApplication.processEvents()
        assert lbl.text().startswith("Y:")
        assert "3.140" in lbl.text()
        assert "9.900" not in lbl.text()

    def test_dro_machine_uses_machine_position(self):
        t, status, cmd, win, (lbl,) = _make_world(
            _state_label("dro_m", "dro_machine", axis=0),
        )
        t.mutate_state(machine_position=Position(x=7.5))
        QApplication.processEvents()
        assert "7.500" in lbl.text()

    def test_dro_dtg_uses_dtg_position(self):
        t, status, cmd, win, (lbl,) = _make_world(
            _state_label("dro_d", "dro_dtg", axis=0),
        )
        t.mutate_state(dtg=Position(x=-2.250))
        QApplication.processEvents()
        assert "-2.250" in lbl.text()

    def test_explicit_format_string_overrides_unit_format(self):
        def factory(window):
            lbl = StateLabel(window)
            lbl.setObjectName("dro_f")
            lbl._set_state("dro_work")
            lbl._set_axis(0)
            lbl._set_format("{letter}={value:.2f}")
            return lbl
        t, status, cmd, win, (lbl,) = _make_world(factory)
        t.mutate_state(position=Position(x=5.0))
        QApplication.processEvents()
        assert lbl.text() == "X=5.00"


# ---------------------------------------------------------------------------
# StateLabel — spindle / tool / feed / overrides
# ---------------------------------------------------------------------------


class TestStateLabelMiscReadouts:
    def test_tool_in_spindle(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("tool", "tool_in_spindle"))
        assert lbl.text() == "T0"
        t.mutate_state(tool_in_spindle=7)
        QApplication.processEvents()
        assert lbl.text() == "T7"

    def test_spindle_speed(self):
        t, status, cmd, win, (lbl,) = _make_world(
            _state_label("sp", "spindle_speed", index=0),
        )
        assert lbl.text() == "0"
        t.mutate_state(spindles=(SpindleState(index=0, speed=1200.0),))
        QApplication.processEvents()
        assert lbl.text() == "1200"

    def test_spindle_direction(self):
        t, status, cmd, win, (lbl,) = _make_world(
            _state_label("sd", "spindle_direction", index=0),
        )
        assert lbl.text() == "STOP"
        t.mutate_state(
            spindles=(
                SpindleState(index=0, speed=500.0, direction=SpindleDir.FORWARD),
            ),
        )
        QApplication.processEvents()
        assert lbl.text() == "FORWARD"

    def test_feed_rate(self):
        t, status, cmd, win, (lbl,) = _make_world(_state_label("fr", "feed_rate"))
        assert lbl.text() == "0.0"
        t.mutate_state(feed_rate=125.5)
        QApplication.processEvents()
        assert lbl.text() == "125.5"

    def test_feed_override(self):
        from qtcnc.core.types import Overrides
        t, status, cmd, win, (lbl,) = _make_world(_state_label("fo", "feed_override"))
        assert lbl.text() == "100%"
        old = t.get_snapshot().overrides
        t.mutate_state(overrides=replace(old, feed=0.75))
        QApplication.processEvents()
        assert lbl.text() == "75%"

    def test_program_line(self):
        from qtcnc.core.types import ProgramState
        t, status, cmd, win, (lbl,) = _make_world(_state_label("pl", "program_line"))
        assert lbl.text() == "0"
        old = t.get_snapshot().program
        t.mutate_state(program=replace(old, current_line=42))
        QApplication.processEvents()
        assert lbl.text() == "42"


# ---------------------------------------------------------------------------
# StateLabel — validation
# ---------------------------------------------------------------------------


class TestStateLabelValidation:
    def test_unknown_state_raises(self, qapp):
        lbl = StateLabel()
        with pytest.raises(ValueError):
            lbl._set_state("not_a_real_state")


# ---------------------------------------------------------------------------
# Composite screen — exercises ActionButton estop + power alongside
# StateLabel readouts to confirm they coexist and the round trip works.
# ---------------------------------------------------------------------------


class TestCompositeScreen:
    def test_widgets_coexist(self):
        def factory_estop(w):
            b = ActionButton(w)
            b.setObjectName("estop")
            b._set_action("estop")
            return b

        def factory_power(w):
            b = ActionButton(w)
            b.setObjectName("power")
            b._set_action("power")
            return b

        dro_factory = _state_label("dro_x", "dro_work", axis=0)
        state_factory = _state_label("state", "task_state")

        t, status, cmd, win, widgets = _make_world(
            factory_estop, factory_power, dro_factory, state_factory,
        )
        estop, power, dro, state = widgets

        assert estop.text() == "ESTOP RESET"
        assert estop.isChecked() is True
        assert power.isEnabled() is False
        assert state.text() == "ESTOPPED"

        estop.click()
        QApplication.processEvents()
        assert state.text() == "ESTOP RESET"
        assert power.isEnabled() is True
        assert estop.isChecked() is False

        power.click()
        QApplication.processEvents()
        assert state.text() == "ON"
        assert power.isChecked() is True
        assert power.text() == "POWER OFF"

        t.mutate_state(position=Position(x=12.345))
        QApplication.processEvents()
        assert "12.345" in dro.text()
        assert dro.text().startswith("X:")
