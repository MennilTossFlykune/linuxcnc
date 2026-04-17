"""Tests for ManualToolChangeDialog."""

from __future__ import annotations

import sys

import pytest

from qtpy.QtWidgets import QApplication, QDialogButtonBox

from qtcnc.core.hal_spec import HalDir, HalType
from qtcnc.widgets.common.manual_tool_change import ManualToolChangeDialog


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class _FakePin:
    def __init__(self, name: str):
        self.name = name
        self._value = None
        self._listeners: list = []

    @property
    def value(self):
        return self._value

    def set(self, v):
        self._value = v

    @property
    def value_changed(self):
        return self

    def connect(self, slot):
        self._listeners.append(slot)

    def emit(self, v):
        for fn in self._listeners:
            fn(v)


class _FakeHub:
    def __init__(self):
        self._pins: dict[str, _FakePin] = {}

    def __getitem__(self, name: str) -> _FakePin:
        if name not in self._pins:
            self._pins[name] = _FakePin(name)
        return self._pins[name]


class TestPinDeclaration:
    def test_declares_three_pins(self):
        pins = ManualToolChangeDialog.declared_pins("manual-tool-change")
        assert len(pins) == 3

    def test_pin_names(self):
        pins = ManualToolChangeDialog.declared_pins("mtc")
        names = {p.name for p in pins}
        assert names == {
            "qtcnc.mtc.change",
            "qtcnc.mtc.changed",
            "qtcnc.mtc.number",
        }

    def test_pin_types(self):
        pins = ManualToolChangeDialog.declared_pins("mtc")
        by_name = {p.name: p for p in pins}
        assert by_name["qtcnc.mtc.change"].type == HalType.BIT
        assert by_name["qtcnc.mtc.change"].dir == HalDir.IN
        assert by_name["qtcnc.mtc.changed"].type == HalType.BIT
        assert by_name["qtcnc.mtc.changed"].dir == HalDir.OUT
        assert by_name["qtcnc.mtc.number"].type == HalType.S32
        assert by_name["qtcnc.mtc.number"].dir == HalDir.IN


class TestDialogProperties:
    def test_is_mandatory(self):
        dlg = ManualToolChangeDialog()
        from qtpy.QtGui import QCloseEvent
        event = QCloseEvent()
        dlg.closeEvent(event)
        assert not event.isAccepted()

    def test_is_blocking(self):
        from qtpy.QtCore import Qt
        dlg = ManualToolChangeDialog()
        assert dlg.windowModality() == Qt.WindowModality.ApplicationModal

    def test_ok_button_says_continue(self):
        dlg = ManualToolChangeDialog()
        btn = dlg.button(QDialogButtonBox.StandardButton.Ok)
        assert btn.text() == "Continue"


class TestTrigger:
    def test_trigger_shows_tool_number(self):
        hub = _FakeHub()
        dlg = ManualToolChangeDialog()
        dlg.hal_setup(hub, "mtc")
        hub["qtcnc.mtc.number"]._value = 7
        shown = []
        dlg.show = lambda: shown.append(True)
        hub["qtcnc.mtc.change"].emit(True)
        QApplication.processEvents()
        assert shown == [True]
        assert "T7" in dlg.header.text()

    def test_trigger_zero_shows_remove(self):
        hub = _FakeHub()
        dlg = ManualToolChangeDialog()
        dlg.hal_setup(hub, "mtc")
        hub["qtcnc.mtc.number"]._value = 0
        dlg.show = lambda: None
        hub["qtcnc.mtc.change"].emit(True)
        QApplication.processEvents()
        assert "Remove" in dlg.header.text()

    def test_false_trigger_does_not_show(self):
        hub = _FakeHub()
        dlg = ManualToolChangeDialog()
        dlg.hal_setup(hub, "mtc")
        shown = []
        dlg.show = lambda: shown.append(True)
        hub["qtcnc.mtc.change"].emit(False)
        QApplication.processEvents()
        assert shown == []

    def test_trigger_drop_before_deferred_show_cancels(self):
        """True then False before the QTimer fires: dialog must not show."""
        hub = _FakeHub()
        dlg = ManualToolChangeDialog()
        dlg.hal_setup(hub, "mtc")
        hub["qtcnc.mtc.number"]._value = 3
        shown = []
        dlg.show = lambda: shown.append(True)
        hub["qtcnc.mtc.change"].emit(True)
        hub["qtcnc.mtc.change"].emit(False)
        QApplication.processEvents()
        assert shown == []
        assert hub["qtcnc.mtc.changed"]._value is not True


class TestResponse:
    def test_accept_sets_changed_true(self):
        hub = _FakeHub()
        dlg = ManualToolChangeDialog()
        dlg.hal_setup(hub, "mtc")
        dlg.accept()
        assert hub["qtcnc.mtc.changed"]._value is True

    def test_reject_sets_changed_false(self):
        hub = _FakeHub()
        dlg = ManualToolChangeDialog()
        dlg.hal_setup(hub, "mtc")
        hub["qtcnc.mtc.changed"]._value = True
        dlg.reject()
        assert hub["qtcnc.mtc.changed"]._value is False

    def test_handshake_drops_changed_after_change_goes_low(self):
        """After accept, iocontrol drops `change`; the dialog must drop
        `changed` so the next M6 starts from a clean handshake."""
        hub = _FakeHub()
        dlg = ManualToolChangeDialog()
        dlg.hal_setup(hub, "mtc")
        hub["qtcnc.mtc.number"]._value = 1
        dlg.show = lambda: None
        hub["qtcnc.mtc.change"].emit(True)
        QApplication.processEvents()
        dlg.accept()
        assert hub["qtcnc.mtc.changed"]._value is True
        hub["qtcnc.mtc.change"].emit(False)
        assert hub["qtcnc.mtc.changed"]._value is False

    def test_second_tool_change_shows_dialog(self):
        """Two M6 cycles in a row: both must show the dialog."""
        hub = _FakeHub()
        dlg = ManualToolChangeDialog()
        dlg.hal_setup(hub, "mtc")
        shown = []
        dlg.show = lambda: shown.append(True)
        hub["qtcnc.mtc.number"]._value = 1
        hub["qtcnc.mtc.change"].emit(True)
        QApplication.processEvents()
        dlg.accept()
        hub["qtcnc.mtc.change"].emit(False)
        hub["qtcnc.mtc.number"]._value = 2
        hub["qtcnc.mtc.change"].emit(True)
        QApplication.processEvents()
        assert shown == [True, True]
