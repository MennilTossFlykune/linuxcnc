"""Tests for qtcnc.transport.qt_marshaller."""

from __future__ import annotations

import sys
import threading
from typing import Any

import pytest

from qtpy.QtCore import QCoreApplication, QObject, Qt, QTimer
from qtpy.QtWidgets import QApplication

from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.types import ErrorMessage, ErrorSeverity, Position
from qtcnc.signals import CommandVerb, Lifecycle
from qtcnc.transport.mock import MockTransport
from qtcnc.transport.qt_marshaller import QtMarshaller, QtMarshalledTransport


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _process_events(timeout_ms: int = 100) -> None:
    """Drain queued slots without blocking the thread."""
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.start(timeout_ms)
    while deadline.isActive():
        QCoreApplication.processEvents()


class TestQtMarshaller:
    def test_wrap_none_returns_none(self):
        m = QtMarshaller()
        assert m.wrap(None) is None

    def test_wrapped_callback_runs_on_owning_thread(self):
        m = QtMarshaller()
        main_tid = threading.get_ident()
        seen: list[int] = []

        def cb(value: int) -> None:
            seen.append(threading.get_ident())

        wrapped = m.wrap(cb)
        assert wrapped is not None

        def trigger() -> None:
            wrapped(42)

        t = threading.Thread(target=trigger)
        t.start()
        t.join()

        # Slot is queued, not yet executed.
        assert seen == []
        _process_events()
        assert seen == [main_tid]

    def test_wrapped_callback_preserves_args_and_kwargs(self):
        m = QtMarshaller()
        captured: list[tuple] = []

        def cb(a: int, b: int, *, k: str) -> None:
            captured.append((a, b, k))

        wrapped = m.wrap(cb)
        assert wrapped is not None
        wrapped(1, 2, k="hello")
        _process_events()
        assert captured == [(1, 2, "hello")]

    def test_multiple_callbacks_are_isolated(self):
        m = QtMarshaller()
        a_seen: list[int] = []
        b_seen: list[int] = []

        wa = m.wrap(lambda x: a_seen.append(x))
        wb = m.wrap(lambda x: b_seen.append(x))
        assert wa is not None and wb is not None

        wa(1)
        wb(2)
        wa(3)
        _process_events()
        assert a_seen == [1, 3]
        assert b_seen == [2]


class TestQtMarshalledTransport:
    def test_sync_methods_passthrough(self):
        inner = MockTransport()
        marshaller = QtMarshaller()
        wrapped = QtMarshalledTransport(inner, marshaller)

        reply = wrapped.hello()
        assert isinstance(reply, dict)
        assert wrapped.is_connected is True

        snap = wrapped.get_snapshot()
        assert snap is not None
        # Mock returns a real StateStore.
        from qtcnc.core.state import StateStore
        assert isinstance(snap, StateStore)

    def test_declare_pins_passthrough(self):
        inner = MockTransport()
        wrapped = QtMarshalledTransport(inner, QtMarshaller())
        wrapped.hello()
        spec = HalPinSpec(name="qtcnc.test.value-out", type=HalType.FLOAT, dir=HalDir.OUT)
        result = wrapped.declare_pins([spec])
        assert result.created == ["qtcnc.test.value-out"]
        assert result.inherited is False

    def test_write_pin_and_exec_command_passthrough(self):
        inner = MockTransport()
        wrapped = QtMarshalledTransport(inner, QtMarshaller())
        wrapped.hello()
        wrapped.exec_command(CommandVerb.ESTOP_RESET)
        wrapped.exec_command(CommandVerb.POWER_ON)
        spec = HalPinSpec(name="qtcnc.dro.value-out", type=HalType.FLOAT, dir=HalDir.OUT)
        wrapped.declare_pins([spec])
        wrapped.write_pin("qtcnc.dro.value-out", 12.345)
        # No exception means the call landed on the inner mock.

    def test_close_passthrough(self):
        inner = MockTransport()
        wrapped = QtMarshalledTransport(inner, QtMarshaller())
        wrapped.hello()
        wrapped.close()
        # Idempotent: no exception on second close.
        wrapped.close()

    def test_inner_property(self):
        inner = MockTransport()
        wrapped = QtMarshalledTransport(inner, QtMarshaller())
        assert wrapped.inner is inner

    def test_set_on_state_diff_marshals_to_main_thread(self):
        inner = MockTransport()
        wrapped = QtMarshalledTransport(inner, QtMarshaller())

        main_tid = threading.get_ident()
        seen: list[tuple[int, list]] = []

        def on_diff(changes: list) -> None:
            seen.append((threading.get_ident(), changes))

        wrapped.set_on_state_diff(on_diff)
        wrapped.hello()

        # Mock dispatches diffs synchronously from whatever thread emits them.
        # Fire from a background thread; assert the slot fires on the main one.
        def trigger() -> None:
            inner.mutate_state_from_changes([("position", Position(x=1.0))])

        t = threading.Thread(target=trigger)
        t.start()
        t.join()

        assert seen == []
        _process_events()
        assert len(seen) == 1
        tid, changes = seen[0]
        assert tid == main_tid
        assert changes and changes[0][0] == "position"

    def test_set_on_error_marshals(self):
        inner = MockTransport()
        wrapped = QtMarshalledTransport(inner, QtMarshaller())
        seen: list[ErrorMessage] = []
        wrapped.set_on_error(seen.append)
        wrapped.hello()

        err = ErrorMessage(severity=ErrorSeverity.OPERATOR_ERROR, text="hi", timestamp=0.0)

        def trigger() -> None:
            inner.inject_error(err)

        t = threading.Thread(target=trigger)
        t.start()
        t.join()

        _process_events()
        assert seen == [err]

    def test_set_on_lifecycle_marshals(self):
        inner = MockTransport()
        wrapped = QtMarshalledTransport(inner, QtMarshaller())
        seen: list[tuple[str, dict]] = []
        wrapped.set_on_lifecycle(lambda tag, payload: seen.append((tag, payload)))
        wrapped.hello()

        def trigger() -> None:
            inner.inject_lifecycle(Lifecycle.PROGRAM_LOADING, {"path": "/tmp/x.ngc"})

        t = threading.Thread(target=trigger)
        t.start()
        t.join()

        _process_events()
        assert len(seen) == 1
        tag, payload = seen[0]
        assert tag == Lifecycle.PROGRAM_LOADING
        assert payload == {"path": "/tmp/x.ngc"}

    def test_setting_none_callback_does_not_blow_up(self):
        inner = MockTransport()
        wrapped = QtMarshalledTransport(inner, QtMarshaller())
        wrapped.set_on_state_diff(None)  # type: ignore[arg-type]
        wrapped.hello()
        # Inner gets None, which it treats as "no callback".
        inner.mutate_state_from_changes([("position", Position(x=1.0))])
        _process_events()
        # Nothing to assert — just no exception.

    def test_start_stop_sub_thread_noop_for_mock(self):
        """MockTransport doesn't have start_background_sub_thread; wrapper
        silently no-ops."""
        inner = MockTransport()
        wrapped = QtMarshalledTransport(inner, QtMarshaller())
        wrapped.start_background_sub_thread()  # no-op
        wrapped.stop_background_sub_thread()  # no-op
