"""End-to-end tests for the v1 MVP widgets in mock mode."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.core.command import Command
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.status import Status
from qtcnc.core.types import Position, TaskMode, TaskState
from qtcnc.signals import CommandVerb
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.base import QtcncWidget, collect_pin_declarations
from qtcnc.widgets.common.dro import DroWidget
from qtcnc.widgets.common.estop_button import EstopButton
from qtcnc.widgets.common.machine_power_button import MachinePowerButton
from qtcnc.widgets.common.state_label import StateLabel
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
    cmd = Command(t)
    window.qtcnc_status = status
    window.qtcnc_command = cmd
    window.qtcnc_hal = hub
    # bootstrap sequence
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
# EstopButton
# ---------------------------------------------------------------------------


class TestEstopButton:
    def _mk(self):
        def factory(window):
            btn = EstopButton(window)
            btn.setObjectName("estop_btn")
            return btn
        return _make_world(factory)

    def test_initial_label_is_estop_reset(self):
        t, status, cmd, win, (btn,) = self._mk()
        # Plausible snapshot has estop=True, so the label should offer to reset.
        assert btn.text() == "ESTOP RESET"

    def test_click_when_estopped_issues_reset(self):
        t, status, cmd, win, (btn,) = self._mk()
        btn.click()
        QApplication.processEvents()
        assert t.get_snapshot().machine.estop is False
        assert btn.text() == "ESTOP"

    def test_click_when_reset_issues_estop(self):
        t, status, cmd, win, (btn,) = self._mk()
        # First release estop
        btn.click(); QApplication.processEvents()
        # Then click again to re-estop
        btn.click(); QApplication.processEvents()
        assert t.get_snapshot().machine.estop is True


# ---------------------------------------------------------------------------
# MachinePowerButton
# ---------------------------------------------------------------------------


class TestMachinePowerButton:
    def _mk(self):
        def factory(window):
            btn = MachinePowerButton(window)
            btn.setObjectName("power_btn")
            return btn
        return _make_world(factory)

    def test_disabled_when_estopped(self):
        t, status, cmd, win, (btn,) = self._mk()
        assert btn.isEnabled() is False

    def test_enabled_after_estop_reset(self):
        t, status, cmd, win, (btn,) = self._mk()
        t.exec_command(CommandVerb.ESTOP_RESET)
        QApplication.processEvents()
        assert btn.isEnabled() is True

    def test_click_toggles_power(self):
        t, status, cmd, win, (btn,) = self._mk()
        t.exec_command(CommandVerb.ESTOP_RESET)
        QApplication.processEvents()
        assert btn.text() == "POWER ON"
        btn.click()
        QApplication.processEvents()
        assert t.get_snapshot().machine.powered is True
        assert btn.text() == "POWER OFF"
        btn.click()
        QApplication.processEvents()
        assert t.get_snapshot().machine.powered is False
        assert btn.text() == "POWER ON"


# ---------------------------------------------------------------------------
# DroWidget
# ---------------------------------------------------------------------------


class TestDroWidget:
    def _mk(self):
        def factory(window):
            dro = DroWidget(window)
            dro.setObjectName("dro_x")
            dro._set_axis(0)
            return dro
        return _make_world(factory)

    def test_declares_hal_pin(self):
        t, status, cmd, win, (dro,) = self._mk()
        # pin should have been registered on the hub
        pin = dro.pin("value-out")
        assert pin.name == "qtcnc.dro_x.value-out"

    def test_label_initially_zero(self):
        t, status, cmd, win, (dro,) = self._mk()
        assert "X:" in dro.text()
        assert "0.0000" in dro.text()

    def test_updates_on_position_change(self):
        t, status, cmd, win, (dro,) = self._mk()
        t.mutate_state(position=Position(x=1.2345))
        QApplication.processEvents()
        assert "1.2345" in dro.text()

    def test_ignores_other_axes(self):
        t, status, cmd, win, (dro,) = self._mk()
        t.mutate_state(position=Position(y=9.9))
        QApplication.processEvents()
        # dro is on axis 0 (X), so should not update from a Y-only change
        assert "9.9" not in dro.text()

    def test_dro_axis_y_only_updates_on_y(self):
        def factory(window):
            dro = DroWidget(window)
            dro.setObjectName("dro_y")
            dro._set_axis(1)
            return dro
        t, status, cmd, win, (dro,) = _make_world(factory)
        t.mutate_state(position=Position(y=3.14))
        QApplication.processEvents()
        assert "Y:" in dro.text()
        assert "3.1400" in dro.text()

    def test_hal_pin_gets_updated_value(self):
        t, status, cmd, win, (dro,) = self._mk()
        t.mutate_state(position=Position(x=2.5))
        QApplication.processEvents()
        assert t.pin_value("qtcnc.dro_x.value-out") == 2.5


# ---------------------------------------------------------------------------
# StateLabel
# ---------------------------------------------------------------------------


class TestStateLabel:
    def _mk(self):
        def factory(window):
            lbl = StateLabel(window)
            lbl.setObjectName("state_lbl")
            return lbl
        return _make_world(factory)

    def test_initial_is_estopped(self):
        t, status, cmd, win, (lbl,) = self._mk()
        assert lbl.text() == "ESTOPPED"

    def test_updates_on_state_change(self):
        t, status, cmd, win, (lbl,) = self._mk()
        t.exec_command(CommandVerb.ESTOP_RESET)
        QApplication.processEvents()
        assert lbl.text() == "ESTOP RESET"
        t.exec_command(CommandVerb.POWER_ON)
        QApplication.processEvents()
        assert lbl.text() == "ON"


# ---------------------------------------------------------------------------
# Composite screen
# ---------------------------------------------------------------------------


class TestCompositeScreen:
    def test_four_widgets_coexist(self):
        def factory_estop(w):
            b = EstopButton(w); b.setObjectName("estop"); return b
        def factory_power(w):
            b = MachinePowerButton(w); b.setObjectName("power"); return b
        def factory_dro(w):
            d = DroWidget(w); d.setObjectName("dro_x"); d._set_axis(0); return d
        def factory_state(w):
            s = StateLabel(w); s.setObjectName("state"); return s

        t, status, cmd, win, widgets = _make_world(
            factory_estop, factory_power, factory_dro, factory_state,
        )
        estop, power, dro, state = widgets
        # Initial: estopped
        assert estop.text() == "ESTOP RESET"
        assert power.isEnabled() is False
        assert state.text() == "ESTOPPED"

        # Reset estop via the button
        estop.click()
        QApplication.processEvents()
        assert state.text() == "ESTOP RESET"
        assert power.isEnabled() is True

        # Power on
        power.click()
        QApplication.processEvents()
        assert state.text() == "ON"

        # Move X axis
        t.mutate_state(position=Position(x=12.3456))
        QApplication.processEvents()
        assert "12.3456" in dro.text()
        assert t.pin_value("qtcnc.dro_x.value-out") == 12.3456
