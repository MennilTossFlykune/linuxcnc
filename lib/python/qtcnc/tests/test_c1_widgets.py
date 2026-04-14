"""C1 widget tests: DroGrid, FileOpenButton, JogPad in mock mode.

Each test stands up the same simplified bootstrap as `test_mvp_widgets`:
MockTransport + Status + Command + HalPinHub + widgets, then exercises
the widget against `MockTransport.mutate_state` / `inject_lifecycle` /
`exec_command` so every assertion is deterministic.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.core.command import Command
from qtcnc.core.status import Status
from qtcnc.core.types import (
    MachineState,
    Position,
    ProgramState,
    TaskMode,
)
from qtcnc.signals import CommandVerb, Lifecycle
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.base import QtcncWidget, collect_pin_declarations
from qtcnc.widgets.common.dro_grid import DroGrid
from qtcnc.widgets.common.file_open_button import FileOpenButton
from qtcnc.widgets.common.jog_pad import JogPad
from qtcnc.widgets.hal import HalPinHub


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _make_world(*widget_factories, axis_count: int = 3):
    """Stand up a mock world. `axis_count` overrides the snapshot's machine
    axis_count before bootstrap so widgets see the correct value when their
    declared_pins() runs."""
    initial = MockTransport().get_snapshot()
    initial = initial.__class__(  # rebuild via dataclass replace
        **{
            **{f.name: getattr(initial, f.name) for f in initial.__dataclass_fields__.values()},
            "machine": MachineState(
                estop=initial.machine.estop,
                powered=initial.machine.powered,
                task_mode=initial.machine.task_mode,
                interp_state=initial.machine.interp_state,
                motion_type=initial.machine.motion_type,
                homed=tuple(False for _ in range(axis_count)),
                axis_count=axis_count,
            ),
        }
    )
    t = MockTransport(initial=initial)
    window = QMainWindow()
    widgets: list[QtcncWidget] = [f(window) for f in widget_factories]
    status = Status(t)
    hub = HalPinHub(t)
    cmd = Command(t)
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


# ---------------------------------------------------------------------------
# DroGrid
# ---------------------------------------------------------------------------


class TestDroGrid:
    def _mk(self, axis_count: int = 3):
        def factory(window):
            grid = DroGrid(window)
            grid.setObjectName("dro")
            return grid
        return _make_world(factory, axis_count=axis_count)

    def test_creates_one_pin_per_axis(self):
        t, status, cmd, win, (grid,) = self._mk(axis_count=3)
        assert grid.pin("x-out").name == "qtcnc.dro.x-out"
        assert grid.pin("y-out").name == "qtcnc.dro.y-out"
        assert grid.pin("z-out").name == "qtcnc.dro.z-out"

    def test_updates_all_axes_on_position_changed(self):
        t, status, cmd, win, (grid,) = self._mk(axis_count=3)
        t.mutate_state(position=Position(x=1.5, y=2.25, z=3.125))
        QApplication.processEvents()
        # Each axis label should reflect the new value.
        assert "1.5000" in grid._value_labels[0].text()
        assert "2.2500" in grid._value_labels[1].text()
        assert "3.1250" in grid._value_labels[2].text()
        # And the HAL pins should be updated.
        assert t.pin_value("qtcnc.dro.x-out") == 1.5
        assert t.pin_value("qtcnc.dro.y-out") == 2.25
        assert t.pin_value("qtcnc.dro.z-out") == 3.125

    def test_axis_count_drives_grid_rows(self):
        t, status, cmd, win, (grid,) = self._mk(axis_count=5)
        # X Y Z A B
        assert len(grid._value_labels) == 5
        assert grid.pin("a-out").name == "qtcnc.dro.a-out"
        assert grid.pin("b-out").name == "qtcnc.dro.b-out"

    def test_machine_coord_system_uses_machine_position_signal(self):
        def factory(window):
            grid = DroGrid(window)
            grid.setObjectName("dro_machine")
            grid._set_coord_system("machine")
            return grid
        t, status, cmd, win, (grid,) = _make_world(factory, axis_count=3)
        # Updating work position should NOT move the machine grid.
        t.mutate_state(position=Position(x=9.9))
        QApplication.processEvents()
        assert "9.9" not in grid._value_labels[0].text()
        # But updating machine_position should.
        t.mutate_state(machine_position=Position(x=4.2))
        QApplication.processEvents()
        assert "4.2000" in grid._value_labels[0].text()

    def test_dtg_coord_system(self):
        def factory(window):
            grid = DroGrid(window)
            grid.setObjectName("dro_dtg")
            grid._set_coord_system("dtg")
            return grid
        t, status, cmd, win, (grid,) = _make_world(factory, axis_count=3)
        t.mutate_state(dtg=Position(x=0.001, y=0.002))
        QApplication.processEvents()
        assert "0.0010" in grid._value_labels[0].text()
        assert "0.0020" in grid._value_labels[1].text()

    def test_unknown_coord_system_raises(self):
        def factory(window):
            return DroGrid(window)
        grid = DroGrid()
        with pytest.raises(ValueError):
            grid._set_coord_system("absolute")


# ---------------------------------------------------------------------------
# FileOpenButton
# ---------------------------------------------------------------------------


class TestFileOpenButton:
    def _mk(self):
        def factory(window):
            btn = FileOpenButton(window)
            btn.setObjectName("file_btn")
            return btn
        return _make_world(factory)

    def test_initial_label(self):
        t, status, cmd, win, (btn,) = self._mk()
        assert btn.text() == "Open G-code"

    def test_program_loaded_updates_label_to_basename(self):
        t, status, cmd, win, (btn,) = self._mk()
        t.inject_lifecycle(
            Lifecycle.PROGRAM_LOADED,
            {"path": "/tmp/job/part.ngc",
             "program": ProgramState(path="/tmp/job/part.ngc", total_lines=42)},
        )
        QApplication.processEvents()
        assert btn.text() == "part.ngc"
        assert btn.toolTip() == "/tmp/job/part.ngc"

    def test_program_load_failed_marks_button_red(self):
        t, status, cmd, win, (btn,) = self._mk()
        t.inject_lifecycle(
            Lifecycle.PROGRAM_LOAD_FAILED,
            {"path": "/tmp/bad.ngc", "reason": "syntax error line 7"},
        )
        QApplication.processEvents()
        assert "background-color" in btn.styleSheet()
        assert "syntax error" in btn.toolTip()

    def test_program_missing_marks_button_red(self):
        t, status, cmd, win, (btn,) = self._mk()
        t.inject_lifecycle(
            Lifecycle.PROGRAM_MISSING,
            {"path": "/tmp/gone.ngc"},
        )
        QApplication.processEvents()
        assert "background-color" in btn.styleSheet()
        assert "gone.ngc" in btn.toolTip()

    def test_seeded_from_existing_program(self):
        def factory(window):
            btn = FileOpenButton(window)
            btn.setObjectName("file_btn")
            return btn
        t = MockTransport()
        t.mutate_state(program=ProgramState(path="/var/loaded.ngc"))
        window = QMainWindow()
        widgets = [factory(window)]
        status = Status(t)
        hub = HalPinHub(t)
        cmd = Command(t)
        window.qtcnc_status = status
        window.qtcnc_command = cmd
        window.qtcnc_hal = hub
        t.hello()
        status.bootstrap()
        for w in widgets:
            w.qtcnc_setup()
        assert widgets[0].text() == "loaded.ngc"


# ---------------------------------------------------------------------------
# JogPad
# ---------------------------------------------------------------------------


class _RecordingMock(MockTransport):
    """MockTransport that captures every exec_command for inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[CommandVerb, dict[str, Any]]] = []

    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        self.commands.append((verb, kwargs))
        super().exec_command(verb, **kwargs)


class TestJogPad:
    def _mk(self, axis_count: int = 3):
        recorder = _RecordingMock()
        # Reset to a powered-on state so jog buttons are enabled.
        s = recorder.get_snapshot()
        recorder.mutate_state(
            machine=MachineState(
                estop=False,
                powered=True,
                task_mode=TaskMode.MANUAL,
                interp_state=s.machine.interp_state,
                motion_type=s.machine.motion_type,
                homed=tuple(True for _ in range(axis_count)),
                axis_count=axis_count,
            ),
        )

        def factory(window):
            pad = JogPad(window)
            pad.setObjectName("jog")
            return pad

        window = QMainWindow()
        widgets = [factory(window)]
        status = Status(recorder)
        hub = HalPinHub(recorder)
        cmd = Command(recorder)
        window.qtcnc_status = status
        window.qtcnc_command = cmd
        window.qtcnc_hal = hub
        recorder.hello()
        status.bootstrap()
        for w in widgets:
            w.qtcnc_setup()
        return recorder, status, cmd, window, widgets[0]

    def test_axis_count_drives_button_count(self):
        rec, status, cmd, win, pad = self._mk(axis_count=4)
        assert len(pad._minus_buttons) == 4
        assert len(pad._plus_buttons) == 4

    def test_continuous_press_release_emits_jog_start_and_stop(self):
        rec, status, cmd, win, pad = self._mk(axis_count=3)
        x_plus = pad._plus_buttons[0]
        x_plus.pressed.emit()
        x_plus.released.emit()
        # Filter to just the jog verbs.
        jogs = [(v, kw) for v, kw in rec.commands
                if v in (CommandVerb.JOG_START, CommandVerb.JOG_STOP)]
        assert len(jogs) == 2
        assert jogs[0][0] == CommandVerb.JOG_START
        assert jogs[0][1] == {"axis": 0, "velocity": 60.0}
        assert jogs[1][0] == CommandVerb.JOG_STOP
        assert jogs[1][1] == {"axis": 0}

    def test_continuous_minus_uses_negative_velocity(self):
        rec, status, cmd, win, pad = self._mk(axis_count=3)
        y_minus = pad._minus_buttons[1]
        y_minus.pressed.emit()
        y_minus.released.emit()
        jogs = [(v, kw) for v, kw in rec.commands if v == CommandVerb.JOG_START]
        assert jogs[0][1] == {"axis": 1, "velocity": -60.0}

    def test_increment_mode_fires_jog_increment_on_release(self):
        rec, status, cmd, win, pad = self._mk(axis_count=3)
        # Switch to increment mode and bump the step size.
        pad._inc_radio.setChecked(True)
        pad._set_increment(0.5)
        z_plus = pad._plus_buttons[2]
        z_plus.pressed.emit()
        # Press alone should NOT issue jog_start in increment mode.
        starts = [v for v, _ in rec.commands if v == CommandVerb.JOG_START]
        assert starts == []
        z_plus.released.emit()
        increments = [(v, kw) for v, kw in rec.commands
                      if v == CommandVerb.JOG_INCREMENT]
        assert len(increments) == 1
        assert increments[0][1] == {"axis": 2, "distance": 0.5, "velocity": 60.0}

    def test_disabled_when_estopped(self):
        rec, status, cmd, win, pad = self._mk(axis_count=3)
        rec.exec_command(CommandVerb.ESTOP)
        QApplication.processEvents()
        assert pad._plus_buttons[0].isEnabled() is False
        assert pad._minus_buttons[0].isEnabled() is False

    def test_disabled_when_powered_off(self):
        rec, status, cmd, win, pad = self._mk(axis_count=3)
        rec.exec_command(CommandVerb.POWER_OFF)
        QApplication.processEvents()
        assert pad._plus_buttons[0].isEnabled() is False

    def test_velocity_property_round_trip(self):
        rec, status, cmd, win, pad = self._mk(axis_count=3)
        pad._set_velocity(120.0)
        assert pad._get_velocity() == 120.0
        # Press X+ — should use the new velocity.
        pad._plus_buttons[0].pressed.emit()
        starts = [(v, kw) for v, kw in rec.commands if v == CommandVerb.JOG_START]
        assert starts[-1][1] == {"axis": 0, "velocity": 120.0}
