"""Tests for messaging widgets: MessageStatusBar, ToastOverlay, MessageLog."""

from __future__ import annotations

import sys
import time

import pytest

from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.core.message_bus import MessageBus
from qtcnc.core.types import Message, MessageSeverity, MessageSource
from qtcnc.widgets.common.message_log import MessageLog
from qtcnc.widgets.common.message_status_bar import MessageStatusBar
from qtcnc.widgets.common.toast_overlay import ToastOverlay


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def window():
    w = QMainWindow()
    bus = MessageBus(parent=w)
    w.qtcnc_messages = bus
    return w


# ---------------------------------------------------------------------------
# MessageStatusBar
# ---------------------------------------------------------------------------


class TestMessageStatusBar:
    def test_info_updates_label(self, window):
        bar = MessageStatusBar(window)
        window.setCentralWidget(bar)
        bar.qtcnc_setup()
        window.qtcnc_messages.post(
            MessageSeverity.INFO, MessageSource.FRAMEWORK, "homing complete",
        )
        assert bar._label.text() == "homing complete"

    def test_error_does_not_reach_bar(self, window):
        bar = MessageStatusBar(window)
        window.setCentralWidget(bar)
        bar.qtcnc_setup()
        window.qtcnc_messages.post(
            MessageSeverity.ERROR, MessageSource.LINUXCNC, "limit tripped",
        )
        assert bar._label.text() == ""

    def test_warning_does_not_reach_bar(self, window):
        bar = MessageStatusBar(window)
        window.setCentralWidget(bar)
        bar.qtcnc_setup()
        window.qtcnc_messages.post(
            MessageSeverity.WARNING, MessageSource.FRAMEWORK, "watch out",
        )
        assert bar._label.text() == ""

    def test_successive_messages_replace(self, window):
        bar = MessageStatusBar(window)
        window.setCentralWidget(bar)
        bar.qtcnc_setup()
        bus = window.qtcnc_messages
        bus.post(MessageSeverity.INFO, MessageSource.USER, "first")
        bus.post(MessageSeverity.INFO, MessageSource.USER, "second")
        assert bar._label.text() == "second"


# ---------------------------------------------------------------------------
# ToastOverlay
# ---------------------------------------------------------------------------


class TestToastOverlay:
    def test_error_creates_toast(self, window):
        overlay = ToastOverlay(window)
        bus = window.qtcnc_messages
        overlay.connect_bus(bus)
        bus.post(MessageSeverity.ERROR, MessageSource.LINUXCNC, "limit switch")
        assert len(overlay._toasts) == 1

    def test_warning_creates_toast(self, window):
        overlay = ToastOverlay(window)
        bus = window.qtcnc_messages
        overlay.connect_bus(bus)
        bus.post(MessageSeverity.WARNING, MessageSource.FRAMEWORK, "watch out")
        assert len(overlay._toasts) == 1

    def test_info_does_not_create_toast(self, window):
        overlay = ToastOverlay(window)
        bus = window.qtcnc_messages
        overlay.connect_bus(bus)
        bus.post(MessageSeverity.INFO, MessageSource.USER, "all good")
        assert len(overlay._toasts) == 0

    def test_max_visible_enforced(self, window):
        overlay = ToastOverlay(window)
        bus = window.qtcnc_messages
        overlay.connect_bus(bus)
        for i in range(5):
            bus.post(MessageSeverity.ERROR, MessageSource.LINUXCNC, f"err {i}")
        assert len(overlay._toasts) == 3

    def test_dismiss_removes_toast(self, window):
        overlay = ToastOverlay(window)
        bus = window.qtcnc_messages
        overlay.connect_bus(bus)
        bus.post(MessageSeverity.ERROR, MessageSource.LINUXCNC, "boom")
        assert len(overlay._toasts) == 1
        overlay._toasts[0]._dismiss()
        assert len(overlay._toasts) == 0


# ---------------------------------------------------------------------------
# MessageLog
# ---------------------------------------------------------------------------


class TestMessageLog:
    def test_all_severities_appear(self, window):
        log = MessageLog(window)
        window.setCentralWidget(log)
        log.qtcnc_setup()
        bus = window.qtcnc_messages
        for sev in MessageSeverity:
            bus.post(sev, MessageSource.FRAMEWORK, f"msg {sev.name}")
        assert log._model.rowCount() == len(MessageSeverity)

    def test_source_filter(self, window):
        log = MessageLog(window)
        window.setCentralWidget(log)
        log.qtcnc_setup()
        bus = window.qtcnc_messages
        bus.post(MessageSeverity.INFO, MessageSource.LINUXCNC, "from lc")
        bus.post(MessageSeverity.INFO, MessageSource.FRAMEWORK, "from fw")
        bus.post(MessageSeverity.INFO, MessageSource.USER, "from user")
        assert log._model.rowCount() == 3

        log._model.set_source_filter(MessageSource.LINUXCNC)
        assert log._model.rowCount() == 1

        log._model.set_source_filter(None)
        assert log._model.rowCount() == 3

    def test_severity_filter(self, window):
        log = MessageLog(window)
        window.setCentralWidget(log)
        log.qtcnc_setup()
        bus = window.qtcnc_messages
        bus.post(MessageSeverity.DEBUG, MessageSource.FRAMEWORK, "debug")
        bus.post(MessageSeverity.INFO, MessageSource.FRAMEWORK, "info")
        bus.post(MessageSeverity.ERROR, MessageSource.FRAMEWORK, "error")
        assert log._model.rowCount() == 3

        log._model.set_severity_filter(MessageSeverity.WARNING)
        assert log._model.rowCount() == 1

        log._model.set_severity_filter(MessageSeverity.INFO)
        assert log._model.rowCount() == 2

        log._model.set_severity_filter(None)
        assert log._model.rowCount() == 3

    def test_cap_at_max_rows(self, window):
        log = MessageLog(window)
        window.setCentralWidget(log)
        log.qtcnc_setup()
        bus = window.qtcnc_messages
        for i in range(1050):
            bus.post(MessageSeverity.INFO, MessageSource.FRAMEWORK, f"msg {i}")
        assert log._model.rowCount() == 1000
