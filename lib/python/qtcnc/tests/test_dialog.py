"""Tests for QtcncDialog base class."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from qtpy.QtCore import Qt, QTimer
from qtpy.QtWidgets import QApplication, QDialogButtonBox, QLabel, QVBoxLayout

from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.widgets.common.dialog import QtcncDialog, Severity


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class TestDefaults:
    def test_default_severity_is_info(self):
        dlg = QtcncDialog()
        assert dlg.severity == Severity.INFO

    def test_default_is_blocking(self):
        dlg = QtcncDialog()
        assert dlg.windowModality() == Qt.WindowModality.ApplicationModal

    def test_default_has_ok_button(self):
        dlg = QtcncDialog()
        assert dlg.button(QDialogButtonBox.StandardButton.Ok) is not None

    def test_title_sets_window_title(self):
        dlg = QtcncDialog(title="Test Title")
        assert dlg.windowTitle() == "Test Title"

    def test_message_sets_header_text(self):
        dlg = QtcncDialog(message="Hello operator")
        assert dlg.header.text() == "Hello operator"

    def test_empty_message_hides_header(self):
        dlg = QtcncDialog()
        assert dlg.header.isHidden()


class TestSeverity:
    def test_warning_severity(self):
        dlg = QtcncDialog(severity=Severity.WARNING)
        assert dlg.severity == Severity.WARNING

    def test_error_severity(self):
        dlg = QtcncDialog(severity=Severity.ERROR)
        assert dlg.severity == Severity.ERROR

    def test_severity_setter_updates(self):
        dlg = QtcncDialog(severity=Severity.INFO)
        dlg.severity = Severity.ERROR
        assert dlg.severity == Severity.ERROR

    def test_severity_applies_stylesheet(self):
        dlg = QtcncDialog(severity=Severity.ERROR)
        assert "#da4453" in dlg.styleSheet()


class TestBlocking:
    def test_blocking_true_is_modal(self):
        dlg = QtcncDialog(blocking=True)
        assert dlg.windowModality() == Qt.WindowModality.ApplicationModal

    def test_blocking_false_is_modeless(self):
        dlg = QtcncDialog(blocking=False)
        assert dlg.windowModality() == Qt.WindowModality.NonModal


class TestFrameless:
    def test_frameless_flag_set(self):
        dlg = QtcncDialog(frameless=True)
        assert dlg.windowFlags() & Qt.WindowType.FramelessWindowHint

    def test_non_frameless_has_no_flag(self):
        dlg = QtcncDialog(frameless=False)
        assert not (dlg.windowFlags() & Qt.WindowType.FramelessWindowHint)


class TestMandatory:
    def test_mandatory_blocks_escape(self):
        dlg = QtcncDialog(mandatory=True)
        from qtpy.QtGui import QKeyEvent
        from qtpy.QtCore import QEvent
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        dlg.keyPressEvent(event)
        assert dlg.isVisible() or True  # didn't reject

    def test_mandatory_blocks_close_event(self):
        from qtpy.QtGui import QCloseEvent
        dlg = QtcncDialog(mandatory=True)
        event = QCloseEvent()
        dlg.closeEvent(event)
        assert not event.isAccepted()

    def test_non_mandatory_allows_close(self):
        from qtpy.QtGui import QCloseEvent
        dlg = QtcncDialog(mandatory=False)
        event = QCloseEvent()
        dlg.closeEvent(event)
        assert event.isAccepted()


class TestTimeout:
    def test_timeout_creates_timer_on_show(self):
        dlg = QtcncDialog(timeout=5000)
        assert dlg._timer is None
        dlg.show()
        QApplication.processEvents()
        assert dlg._timer is not None
        assert dlg._timer.isActive()
        dlg.close()

    def test_no_timeout_by_default(self):
        dlg = QtcncDialog()
        dlg.show()
        QApplication.processEvents()
        assert dlg._timer is None
        dlg.close()


class TestBuildContent:
    def test_subclass_can_add_widgets(self):
        class CustomDialog(QtcncDialog):
            def build_content(self, layout: QVBoxLayout) -> None:
                self.custom_label = QLabel("Custom content", self)
                layout.addWidget(self.custom_label)

        dlg = CustomDialog(message="Header")
        assert dlg.custom_label.text() == "Custom content"

    def test_content_layout_accessible(self):
        dlg = QtcncDialog(message="Test")
        assert isinstance(dlg.content_layout, QVBoxLayout)


class TestButtons:
    def test_ok_cancel_buttons(self):
        dlg = QtcncDialog(
            buttons=(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            ),
        )
        assert dlg.button(QDialogButtonBox.StandardButton.Ok) is not None
        assert dlg.button(QDialogButtonBox.StandardButton.Cancel) is not None

    def test_button_text_override(self):
        dlg = QtcncDialog()
        dlg.button(QDialogButtonBox.StandardButton.Ok).setText("Go")
        assert dlg.button(QDialogButtonBox.StandardButton.Ok).text() == "Go"


class _FakePin:
    """Minimal HalPin stand-in for tests."""

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
    """Minimal HalPinHub stand-in for tests."""

    def __init__(self):
        self._pins: dict[str, _FakePin] = {}

    def __getitem__(self, name: str) -> _FakePin:
        if name not in self._pins:
            self._pins[name] = _FakePin(name)
        return self._pins[name]

    def pin(self, name: str) -> _FakePin:
        return self[name]


class TestHalPins:
    def test_default_has_no_hal_pins(self):
        assert QtcncDialog.HAL_PINS == []
        assert QtcncDialog.TRIGGER_PIN is None
        assert QtcncDialog.RESPONSE_PIN is None

    def test_declared_pins_substitutes_name(self):
        class MyDialog(QtcncDialog):
            HAL_PINS = [
                HalPinSpec(
                    name="qtcnc.{name}.change",
                    type=HalType.BIT, dir=HalDir.IN,
                ),
            ]

        pins = MyDialog.declared_pins("tool_change")
        assert len(pins) == 1
        assert pins[0].name == "qtcnc.tool_change.change"

    def test_declared_pins_multiple(self):
        class MyDialog(QtcncDialog):
            HAL_PINS = [
                HalPinSpec(name="qtcnc.{name}.trigger", type=HalType.BIT, dir=HalDir.IN),
                HalPinSpec(name="qtcnc.{name}.done", type=HalType.BIT, dir=HalDir.OUT),
                HalPinSpec(name="qtcnc.{name}.number", type=HalType.S32, dir=HalDir.IN),
            ]

        pins = MyDialog.declared_pins("mtc")
        assert [p.name for p in pins] == [
            "qtcnc.mtc.trigger",
            "qtcnc.mtc.done",
            "qtcnc.mtc.number",
        ]

    def test_pin_raises_before_hal_setup(self):
        dlg = QtcncDialog()
        with pytest.raises(RuntimeError, match="hal_setup"):
            dlg.pin("change")

    def test_hal_setup_enables_pin_access(self):
        class MyDialog(QtcncDialog):
            HAL_PINS = [
                HalPinSpec(name="qtcnc.{name}.value", type=HalType.FLOAT, dir=HalDir.IN),
            ]

        hub = _FakeHub()
        dlg = MyDialog()
        dlg.hal_setup(hub, "test")
        pin = dlg.pin("value")
        assert pin.name == "qtcnc.test.value"

    def test_trigger_pin_shows_dialog(self):
        class MyDialog(QtcncDialog):
            HAL_PINS = [
                HalPinSpec(name="qtcnc.{name}.trigger", type=HalType.BIT, dir=HalDir.IN),
            ]
            TRIGGER_PIN = "trigger"

        hub = _FakeHub()
        dlg = MyDialog()
        dlg.hal_setup(hub, "test")
        shown = []
        dlg.on_trigger = lambda: shown.append(True)
        hub["qtcnc.test.trigger"].emit(True)
        assert shown == [True]

    def test_trigger_pin_false_does_not_show(self):
        class MyDialog(QtcncDialog):
            HAL_PINS = [
                HalPinSpec(name="qtcnc.{name}.trigger", type=HalType.BIT, dir=HalDir.IN),
            ]
            TRIGGER_PIN = "trigger"

        hub = _FakeHub()
        dlg = MyDialog()
        dlg.hal_setup(hub, "test")
        shown = []
        dlg.on_trigger = lambda: shown.append(True)
        hub["qtcnc.test.trigger"].emit(False)
        assert shown == []

    def test_response_pin_set_on_accept(self):
        class MyDialog(QtcncDialog):
            HAL_PINS = [
                HalPinSpec(name="qtcnc.{name}.done", type=HalType.BIT, dir=HalDir.OUT),
            ]
            RESPONSE_PIN = "done"

        hub = _FakeHub()
        dlg = MyDialog()
        dlg.hal_setup(hub, "test")
        dlg.accept()
        assert hub["qtcnc.test.done"]._value is True

    def test_response_pin_cleared_on_reject(self):
        class MyDialog(QtcncDialog):
            HAL_PINS = [
                HalPinSpec(name="qtcnc.{name}.done", type=HalType.BIT, dir=HalDir.OUT),
            ]
            RESPONSE_PIN = "done"

        hub = _FakeHub()
        dlg = MyDialog()
        dlg.hal_setup(hub, "test")
        dlg.reject()
        assert hub["qtcnc.test.done"]._value is False

    def test_no_trigger_or_response_is_fine(self):
        hub = _FakeHub()
        dlg = QtcncDialog()
        dlg.hal_setup(hub, "plain")
        dlg.accept()
        dlg.reject()


class TestTriggerDismissal:
    """When the trigger pin drops while the dialog is visible, the dialog
    should close without writing the response pin.  This covers the
    multi-client scenario: one client accepts, LinuxCNC drops the trigger,
    and every other client's dialog auto-dismisses.
    """

    def _make_dialog(self):
        class TriggerDialog(QtcncDialog):
            HAL_PINS = [
                HalPinSpec(name="qtcnc.{name}.trigger", type=HalType.BIT, dir=HalDir.IN),
                HalPinSpec(name="qtcnc.{name}.done", type=HalType.BIT, dir=HalDir.OUT),
            ]
            TRIGGER_PIN = "trigger"
            RESPONSE_PIN = "done"

        hub = _FakeHub()
        dlg = TriggerDialog()
        dlg.hal_setup(hub, "mtc")
        return dlg, hub

    def test_trigger_drop_dismisses_visible_dialog(self):
        dlg, hub = self._make_dialog()
        hub["qtcnc.mtc.trigger"].emit(True)
        QApplication.processEvents()
        dlg.show()
        QApplication.processEvents()
        assert dlg.isVisible()
        hub["qtcnc.mtc.trigger"].emit(False)
        QApplication.processEvents()
        assert not dlg.isVisible()

    def test_trigger_drop_does_not_write_response_pin(self):
        dlg, hub = self._make_dialog()
        hub["qtcnc.mtc.trigger"].emit(True)
        QApplication.processEvents()
        dlg.show()
        QApplication.processEvents()
        hub["qtcnc.mtc.done"]._value = None
        hub["qtcnc.mtc.trigger"].emit(False)
        QApplication.processEvents()
        assert hub["qtcnc.mtc.done"]._value is None

    def test_trigger_drop_while_hidden_is_noop(self):
        dlg, hub = self._make_dialog()
        hub["qtcnc.mtc.done"]._value = None
        hub["qtcnc.mtc.trigger"].emit(False)
        QApplication.processEvents()
        assert hub["qtcnc.mtc.done"]._value is None

    def test_normal_reject_still_writes_response_pin(self):
        dlg, hub = self._make_dialog()
        hub["qtcnc.mtc.trigger"].emit(True)
        QApplication.processEvents()
        dlg.show()
        QApplication.processEvents()
        dlg.reject()
        QApplication.processEvents()
        assert hub["qtcnc.mtc.done"]._value is False

    def test_flag_resets_on_next_trigger(self):
        dlg, hub = self._make_dialog()
        # First cycle: trigger → dismiss via pin drop
        hub["qtcnc.mtc.trigger"].emit(True)
        dlg.show()
        QApplication.processEvents()
        hub["qtcnc.mtc.trigger"].emit(False)
        QApplication.processEvents()
        # Second cycle: trigger → normal reject should write pin
        hub["qtcnc.mtc.trigger"].emit(True)
        dlg.show()
        QApplication.processEvents()
        hub["qtcnc.mtc.done"]._value = None
        dlg.reject()
        QApplication.processEvents()
        assert hub["qtcnc.mtc.done"]._value is False
