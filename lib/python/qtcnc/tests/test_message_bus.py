"""Tests for qtcnc.core.message_bus."""

from __future__ import annotations

import sys
import time

import pytest

from qtpy.QtWidgets import QApplication

from qtcnc.core.message_bus import MessageBus
from qtcnc.core.types import (
    ErrorMessage,
    ErrorSeverity,
    Message,
    MessageSeverity,
    MessageSource,
)
from qtcnc.core.status import Status
from qtcnc.transport.mock import MockTransport


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def bus():
    return MessageBus()


class TestRouting:
    def test_error_goes_to_toast_and_log(self, bus):
        toast, log, bar = [], [], []
        bus.toast_message.connect(toast.append)
        bus.log_message.connect(log.append)
        bus.bar_message.connect(bar.append)

        bus.post(MessageSeverity.ERROR, MessageSource.FRAMEWORK, "boom")
        assert len(toast) == 1
        assert len(log) == 1
        assert len(bar) == 0
        assert toast[0].text == "boom"
        assert toast[0].severity == MessageSeverity.ERROR

    def test_warning_goes_to_toast_and_log(self, bus):
        toast, log, bar = [], [], []
        bus.toast_message.connect(toast.append)
        bus.log_message.connect(log.append)
        bus.bar_message.connect(bar.append)

        bus.post(MessageSeverity.WARNING, MessageSource.LINUXCNC, "watch out")
        assert len(toast) == 1
        assert len(log) == 1
        assert len(bar) == 0

    def test_info_goes_to_bar_and_log(self, bus):
        toast, log, bar = [], [], []
        bus.toast_message.connect(toast.append)
        bus.log_message.connect(log.append)
        bus.bar_message.connect(bar.append)

        bus.post(MessageSeverity.INFO, MessageSource.USER, "homing complete")
        assert len(toast) == 0
        assert len(log) == 1
        assert len(bar) == 1
        assert bar[0].text == "homing complete"

    def test_debug_goes_to_log_only(self, bus):
        toast, log, bar = [], [], []
        bus.toast_message.connect(toast.append)
        bus.log_message.connect(log.append)
        bus.bar_message.connect(bar.append)

        bus.post(MessageSeverity.DEBUG, MessageSource.FRAMEWORK, "nack detail")
        assert len(toast) == 0
        assert len(bar) == 0
        assert len(log) == 1
        assert log[0].severity == MessageSeverity.DEBUG


class TestPostCreatesMessage:
    def test_timestamp_is_recent(self, bus):
        log = []
        bus.log_message.connect(log.append)
        before = time.time()
        bus.post(MessageSeverity.INFO, MessageSource.USER, "hello")
        after = time.time()
        assert before <= log[0].timestamp <= after

    def test_source_preserved(self, bus):
        log = []
        bus.log_message.connect(log.append)
        bus.post(MessageSeverity.ERROR, MessageSource.LINUXCNC, "err")
        assert log[0].source == MessageSource.LINUXCNC


class TestStatusIntegration:
    def test_error_signal_routes_through_bus(self):
        t = MockTransport()
        status = Status(t)
        bus = MessageBus()
        bus.connect_status(status)

        toast, log = [], []
        bus.toast_message.connect(toast.append)
        bus.log_message.connect(log.append)

        err = ErrorMessage(
            severity=ErrorSeverity.OPERATOR_ERROR,
            text="limit switch tripped",
            timestamp=time.time(),
        )
        t._dispatch_error(err)

        assert len(toast) == 1
        assert toast[0].severity == MessageSeverity.ERROR
        assert toast[0].source == MessageSource.LINUXCNC
        assert toast[0].text == "limit switch tripped"
        assert len(log) == 1

    def test_info_error_routes_to_bar(self):
        t = MockTransport()
        status = Status(t)
        bus = MessageBus()
        bus.connect_status(status)

        bar = []
        bus.bar_message.connect(bar.append)

        err = ErrorMessage(
            severity=ErrorSeverity.INFO,
            text="all fine",
            timestamp=time.time(),
        )
        t._dispatch_error(err)

        assert len(bar) == 1
        assert bar[0].severity == MessageSeverity.INFO

    def test_operator_display_routes_to_bar(self):
        t = MockTransport()
        status = Status(t)
        bus = MessageBus()
        bus.connect_status(status)

        bar, toast = [], []
        bus.bar_message.connect(bar.append)
        bus.toast_message.connect(toast.append)

        err = ErrorMessage(
            severity=ErrorSeverity.OPERATOR_DISPLAY,
            text="display msg",
            timestamp=time.time(),
        )
        t._dispatch_error(err)

        assert len(bar) == 1
        assert len(toast) == 0

    def test_nml_error_routes_to_toast(self):
        t = MockTransport()
        status = Status(t)
        bus = MessageBus()
        bus.connect_status(status)

        toast = []
        bus.toast_message.connect(toast.append)

        err = ErrorMessage(
            severity=ErrorSeverity.NML_ERROR,
            text="nml fail",
            timestamp=time.time(),
        )
        t._dispatch_error(err)

        assert len(toast) == 1
        assert toast[0].severity == MessageSeverity.ERROR
