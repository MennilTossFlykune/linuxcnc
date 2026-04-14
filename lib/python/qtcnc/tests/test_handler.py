"""Tests for qtcnc.handler — QtcncHandler, WidgetTree, auto_connect."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from qtpy.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QWidget

from qtcnc.core.command import Command
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.status import Status
from qtcnc.core.types import ErrorSeverity, ProgramState, TaskMode
from qtcnc.handler import (
    HandlerContext,
    QtcncHandler,
    WidgetTree,
    auto_connect_handler,
)
from qtcnc.signals import CommandVerb, Lifecycle
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.hal import HalPinHub


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


# ---------------------------------------------------------------------------
# WidgetTree
# ---------------------------------------------------------------------------


class TestWidgetTree:
    def test_finds_child_by_object_name(self):
        root = QMainWindow()
        label = QLabel(root)
        label.setObjectName("mylabel")
        tree = WidgetTree(root)
        assert tree.mylabel is label

    def test_caches_lookups(self):
        root = QMainWindow()
        label = QLabel(root)
        label.setObjectName("mylabel")
        tree = WidgetTree(root)
        a = tree.mylabel
        b = tree.mylabel
        assert a is b

    def test_unknown_name_raises(self):
        root = QMainWindow()
        tree = WidgetTree(root)
        with pytest.raises(AttributeError):
            _ = tree.nope

    def test_contains(self):
        root = QMainWindow()
        btn = QPushButton(root)
        btn.setObjectName("ok_btn")
        tree = WidgetTree(root)
        assert "ok_btn" in tree
        assert "missing" not in tree


# ---------------------------------------------------------------------------
# Default QtcncHandler
# ---------------------------------------------------------------------------


def _build_context():
    t = MockTransport()
    t.hello()
    status = Status(t)
    status.bootstrap()
    hub = HalPinHub(t)
    cmd = Command(t)
    window = QMainWindow()
    tree = WidgetTree(window)
    ctx = HandlerContext(
        window=window,
        status=status,
        command=cmd,
        widgets=tree,
        hal=hub,
        config=None,
    )
    return ctx, t, status


class TestDefaultHandler:
    def test_default_hooks_are_noops(self):
        ctx, _, _ = _build_context()
        h = QtcncHandler(ctx)
        # Every default should return None without raising.
        assert h.declare_pins() == []
        h.on_startup()
        h.on_ready()
        h.on_shutdown()
        h.on_estop(True)
        h.on_power(False)
        h.on_mode_changed(TaskMode.AUTO)
        h.on_error(ErrorSeverity.INFO, "x")
        h.on_file_loaded("/f", ProgramState())
        h.on_file_load_failed("/f", "x")
        h.on_file_closed()
        h.on_file_missing("/f")
        h.on_disconnected("x")
        h.on_reconnected()

    def test_context_attributes(self):
        ctx, _, _ = _build_context()
        h = QtcncHandler(ctx)
        assert h.status is ctx.status
        assert h.cmd is ctx.command
        assert h.w is ctx.widgets
        assert h.hal is ctx.hal
        assert h.window is ctx.window


# ---------------------------------------------------------------------------
# Auto-connect
# ---------------------------------------------------------------------------


class _RecordingHandler(QtcncHandler):
    def __init__(self, ctx: HandlerContext) -> None:
        super().__init__(ctx)
        self.seen: list[tuple[str, tuple]] = []

    def on_estop(self, asserted: bool) -> None:
        self.seen.append(("on_estop", (asserted,)))

    def on_power(self, on: bool) -> None:
        self.seen.append(("on_power", (on,)))

    def on_mode_changed(self, mode: TaskMode) -> None:
        self.seen.append(("on_mode_changed", (mode,)))

    def on_error(self, severity: ErrorSeverity, text: str) -> None:
        self.seen.append(("on_error", (severity, text)))

    def on_file_loaded(self, path: str, program: ProgramState) -> None:
        self.seen.append(("on_file_loaded", (path, program)))

    def on_file_load_failed(self, path: str, reason: str) -> None:
        self.seen.append(("on_file_load_failed", (path, reason)))

    def on_file_closed(self) -> None:
        self.seen.append(("on_file_closed", ()))

    def on_file_missing(self, path: str) -> None:
        self.seen.append(("on_file_missing", (path,)))

    def on_disconnected(self, reason: str) -> None:
        self.seen.append(("on_disconnected", (reason,)))

    def on_reconnected(self) -> None:
        self.seen.append(("on_reconnected", ()))


class TestAutoConnect:
    def test_overridden_methods_get_wired(self):
        ctx, t, status = _build_context()
        h = _RecordingHandler(ctx)
        connections = auto_connect_handler(h)
        # 10 on_* methods overridden → 10 connections
        assert len(connections) == 10

    def test_estop_reset_invokes_on_estop(self):
        ctx, t, status = _build_context()
        h = _RecordingHandler(ctx)
        auto_connect_handler(h)
        t.exec_command(CommandVerb.ESTOP_RESET)
        QApplication.processEvents()
        calls = [c for c in h.seen if c[0] == "on_estop"]
        assert calls == [("on_estop", (False,))]

    def test_power_on_invokes_on_power(self):
        ctx, t, status = _build_context()
        h = _RecordingHandler(ctx)
        auto_connect_handler(h)
        t.exec_command(CommandVerb.ESTOP_RESET)
        t.exec_command(CommandVerb.POWER_ON)
        QApplication.processEvents()
        calls = [c for c in h.seen if c[0] == "on_power"]
        assert calls == [("on_power", (True,))]

    def test_mode_change_invokes_on_mode_changed(self):
        ctx, t, status = _build_context()
        h = _RecordingHandler(ctx)
        auto_connect_handler(h)
        t.exec_command(CommandVerb.SET_MODE, mode=TaskMode.AUTO)
        QApplication.processEvents()
        calls = [c for c in h.seen if c[0] == "on_mode_changed"]
        assert calls == [("on_mode_changed", (TaskMode.AUTO,))]

    def test_error_invokes_on_error(self):
        ctx, t, status = _build_context()
        h = _RecordingHandler(ctx)
        auto_connect_handler(h)
        from qtcnc.core.types import ErrorMessage
        t.inject_error(ErrorMessage(ErrorSeverity.OPERATOR_ERROR, "boom", 1.0))
        QApplication.processEvents()
        assert ("on_error", (ErrorSeverity.OPERATOR_ERROR, "boom")) in h.seen

    def test_file_loaded_invokes_hook(self):
        ctx, t, status = _build_context()
        h = _RecordingHandler(ctx)
        auto_connect_handler(h)
        prog = ProgramState(path="/f.ngc", total_lines=5)
        t.inject_lifecycle(Lifecycle.PROGRAM_LOADED, {"path": "/f.ngc", "program": prog})
        QApplication.processEvents()
        calls = [c for c in h.seen if c[0] == "on_file_loaded"]
        assert calls == [("on_file_loaded", ("/f.ngc", prog))]

    def test_program_closed_hook(self):
        ctx, t, status = _build_context()
        h = _RecordingHandler(ctx)
        auto_connect_handler(h)
        t.inject_lifecycle(Lifecycle.PROGRAM_CLOSED, {})
        QApplication.processEvents()
        assert ("on_file_closed", ()) in h.seen

    def test_program_missing_hook(self):
        ctx, t, status = _build_context()
        h = _RecordingHandler(ctx)
        auto_connect_handler(h)
        t.inject_lifecycle(Lifecycle.PROGRAM_MISSING, {"path": "/gone"})
        QApplication.processEvents()
        assert ("on_file_missing", ("/gone",)) in h.seen

    def test_disconnect_hook(self):
        ctx, t, status = _build_context()
        h = _RecordingHandler(ctx)
        auto_connect_handler(h)
        t.close()
        QApplication.processEvents()
        assert ("on_disconnected", ("closed",)) in h.seen

    def test_non_overridden_methods_not_connected(self):
        ctx, t, status = _build_context()

        class MinimalHandler(QtcncHandler):
            def __init__(self, ctx: HandlerContext) -> None:
                super().__init__(ctx)
                self.seen = False

            def on_estop(self, asserted: bool) -> None:
                self.seen = True

        h = MinimalHandler(ctx)
        connections = auto_connect_handler(h)
        # Only one method overridden → one connection.
        assert len(connections) == 1

    def test_default_handler_makes_no_connections(self):
        ctx, t, status = _build_context()
        h = QtcncHandler(ctx)
        connections = auto_connect_handler(h)
        assert connections == []
