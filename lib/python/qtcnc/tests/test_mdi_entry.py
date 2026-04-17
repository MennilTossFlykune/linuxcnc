"""Tests for MdiEntry and the mdi_entry ActionButton action."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from qtpy.QtCore import Qt
from qtpy.QtGui import QKeyEvent
from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.core.command import Command
from qtcnc.core.status import Status
from qtcnc.core.types import InterpState, MachineState, TaskMode
from qtcnc.signals import CommandVerb
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.common.action_button import ActionButton
from qtcnc.widgets.common.mdi_entry import MdiEntry
from qtcnc.widgets.hal import HalPinHub


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class _RecordingMock(MockTransport):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[CommandVerb, dict[str, Any]]] = []

    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        self.commands.append((verb, kwargs))
        super().exec_command(verb, **kwargs)


def _ready_mdi() -> MachineState:
    return MachineState(
        estop=False, powered=True,
        task_mode=TaskMode.MDI, interp_state=InterpState.IDLE,
        homed=(True, True, True), axis_count=3, axis_mask=0b111,
    )


def _make_world(*widgets, machine: MachineState | None = None):
    t = _RecordingMock()
    if machine is not None:
        t.mutate_state(machine=machine)
    window = QMainWindow()
    for w in widgets:
        w.setParent(window)
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
        w._test_world = window
    return t, status, cmd, window


# ---------------------------------------------------------------------------
# MdiEntry construction + Qt properties
# ---------------------------------------------------------------------------


class TestMdiEntryProperties:
    def test_defaults(self):
        e = MdiEntry()
        assert e.submit_on_enter is True
        assert e.clear_on_submit is True

    def test_submit_on_enter_property_round_trips(self):
        e = MdiEntry()
        e._set_submit_on_enter(False)
        assert e.submit_on_enter is False
        e._set_submit_on_enter(True)
        assert e.submit_on_enter is True

    def test_clear_on_submit_property_round_trips(self):
        e = MdiEntry()
        e._set_clear_on_submit(False)
        assert e.clear_on_submit is False
        e._set_clear_on_submit(True)
        assert e.clear_on_submit is True


# ---------------------------------------------------------------------------
# Enable gating
# ---------------------------------------------------------------------------


class TestMdiEntryEnableGate:
    def test_disabled_when_estopped(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        _make_world(e)
        assert e.isEnabled() is False

    def test_disabled_in_manual_mode(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        t, *_ = _make_world(e, machine=MachineState(
            estop=False, powered=True,
            task_mode=TaskMode.MANUAL, interp_state=InterpState.IDLE,
            axis_count=3, axis_mask=0b111,
        ))
        assert e.isEnabled() is False

    def test_enabled_when_ready_and_mdi_idle(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        _make_world(e, machine=_ready_mdi())
        assert e.isEnabled() is True

    def test_disabled_when_interp_not_idle(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        t, *_ = _make_world(e, machine=_ready_mdi())
        assert e.isEnabled() is True
        t.mutate_state(machine=MachineState(
            estop=False, powered=True,
            task_mode=TaskMode.MDI, interp_state=InterpState.READING,
            homed=(True, True, True), axis_count=3, axis_mask=0b111,
        ))
        QApplication.processEvents()
        assert e.isEnabled() is False

    def test_re_enables_after_interp_goes_idle(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        t, *_ = _make_world(e, machine=MachineState(
            estop=False, powered=True,
            task_mode=TaskMode.MDI, interp_state=InterpState.READING,
            homed=(True, True, True), axis_count=3, axis_mask=0b111,
        ))
        assert e.isEnabled() is False
        t.mutate_state(machine=_ready_mdi())
        QApplication.processEvents()
        assert e.isEnabled() is True


# ---------------------------------------------------------------------------
# execute()
# ---------------------------------------------------------------------------


class TestMdiEntryExecute:
    def test_execute_dispatches_mdi(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        t, *_ = _make_world(e, machine=_ready_mdi())
        e.setText("G0 X5")
        e.execute()
        mdi_cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.MDI]
        assert mdi_cmds[-1][1] == {"command": "G0 X5"}

    def test_execute_clears_by_default(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        _make_world(e, machine=_ready_mdi())
        e.setText("G0 X5")
        e.execute()
        assert e.text() == ""

    def test_execute_respects_clear_on_submit_false(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        e._set_clear_on_submit(False)
        _make_world(e, machine=_ready_mdi())
        e.setText("G0 X5")
        e.execute()
        assert e.text() == "G0 X5"

    def test_execute_strips_whitespace_and_skips_empty(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        t, *_ = _make_world(e, machine=_ready_mdi())
        e.setText("   ")
        e.execute()
        assert CommandVerb.MDI not in [v for v, _ in t.commands]

    def test_execute_noop_when_not_mdi_ready(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        t, *_ = _make_world(e)
        e.setText("G0 X5")
        e.execute()
        assert CommandVerb.MDI not in [v for v, _ in t.commands]
        assert e.text() == "G0 X5"


# ---------------------------------------------------------------------------
# Enter key behaviour
# ---------------------------------------------------------------------------


class TestMdiEntryEnterKey:
    def test_return_pressed_submits_by_default(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        t, *_ = _make_world(e, machine=_ready_mdi())
        e.setText("G1 X0 F100")
        e.returnPressed.emit()
        mdi_cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.MDI]
        assert mdi_cmds[-1][1] == {"command": "G1 X0 F100"}

    def test_return_pressed_ignored_when_submit_on_enter_false(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        e._set_submit_on_enter(False)
        t, *_ = _make_world(e, machine=_ready_mdi())
        e.setText("G1 X0 F100")
        e.returnPressed.emit()
        assert CommandVerb.MDI not in [v for v, _ in t.commands]
        assert e.text() == "G1 X0 F100"

    def test_keypress_enter_runs_through_returnPressed(self):
        e = MdiEntry()
        e.setObjectName("mdi")
        t, *_ = _make_world(e, machine=_ready_mdi())
        e.setText("M3 S1000")
        key = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
        QApplication.sendEvent(e, key)
        mdi_cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.MDI]
        assert mdi_cmds[-1][1] == {"command": "M3 S1000"}


# ---------------------------------------------------------------------------
# mdi_entry ActionButton pairing
# ---------------------------------------------------------------------------


class TestMdiEntryActionButton:
    def _pair(self, machine: MachineState | None = None):
        entry = MdiEntry()
        entry.setObjectName("mdi")
        btn = ActionButton()
        btn._set_action("mdi_entry")
        btn._set_target("mdi")
        t, *_ = _make_world(entry, btn, machine=machine)
        return t, entry, btn

    def test_click_forwards_to_entry_execute(self):
        t, entry, btn = self._pair(machine=_ready_mdi())
        entry.setText("G0 X5")
        btn.click()
        mdi_cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.MDI]
        assert mdi_cmds[-1][1] == {"command": "G0 X5"}
        assert entry.text() == ""

    def test_button_disabled_outside_mdi(self):
        t, entry, btn = self._pair()
        assert btn.isEnabled() is False

    def test_button_enabled_in_mdi_ready(self):
        t, entry, btn = self._pair(machine=_ready_mdi())
        assert btn.isEnabled() is True

    def test_click_with_empty_target_noops(self):
        btn = ActionButton()
        btn._set_action("mdi_entry")
        _make_world(btn, machine=_ready_mdi())
        t = btn.window().qtcnc_status._transport
        btn.click()
        assert CommandVerb.MDI not in [v for v, _ in getattr(t, "commands", [])]

    def test_click_with_empty_entry_noops(self):
        t, entry, btn = self._pair(machine=_ready_mdi())
        entry.setText("")
        btn.click()
        assert CommandVerb.MDI not in [v for v, _ in t.commands]
