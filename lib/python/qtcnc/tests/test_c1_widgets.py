"""C1 widget tests: DroGrid in mock mode.

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
from qtcnc.widgets.hal import HalPinHub


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _make_world(*widget_factories, axis_count: int = 3, axis_mask: int | None = None):
    """Stand up a mock world. `axis_count` overrides the snapshot's machine
    axis_count before bootstrap so widgets see the correct value when their
    declared_pins() runs. `axis_mask` defaults to the low `axis_count` bits
    so the simple trivial-kinematics case doesn't need the caller to
    specify both; gantry configs pass explicit values for each."""
    if axis_mask is None:
        axis_mask = (1 << axis_count) - 1
    initial = MockTransport().get_snapshot()
    initial = initial.__class__(
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
                linear_units=initial.machine.linear_units,
                axis_mask=axis_mask,
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
        assert "1.500" in grid._value_labels[0].text()
        assert "2.250" in grid._value_labels[1].text()
        assert "3.125" in grid._value_labels[2].text()
        assert t.pin_value("qtcnc.dro.x-out") == 1.5
        assert t.pin_value("qtcnc.dro.y-out") == 2.25
        assert t.pin_value("qtcnc.dro.z-out") == 3.125

    def test_axis_count_drives_grid_rows(self):
        t, status, cmd, win, (grid,) = self._mk(axis_count=5)
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
        t.mutate_state(position=Position(x=9.9))
        QApplication.processEvents()
        assert "9.9" not in grid._value_labels[0].text()
        t.mutate_state(machine_position=Position(x=4.2))
        QApplication.processEvents()
        assert "4.200" in grid._value_labels[0].text()

    def test_dtg_coord_system(self):
        def factory(window):
            grid = DroGrid(window)
            grid.setObjectName("dro_dtg")
            grid._set_coord_system("dtg")
            return grid
        t, status, cmd, win, (grid,) = _make_world(factory, axis_count=3)
        t.mutate_state(dtg=Position(x=0.001, y=0.002))
        QApplication.processEvents()
        assert "0.001" in grid._value_labels[0].text()
        assert "0.002" in grid._value_labels[1].text()

    def test_unknown_coord_system_raises(self):
        grid = DroGrid()
        with pytest.raises(ValueError):
            grid._set_coord_system("absolute")
