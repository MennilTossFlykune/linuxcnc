"""Standard screen handler — the v2 demo.

Layout exercise: every widget type the framework ships. The `main.ui`
drops a top bar (estop, power, state, file-open), a left column (DRO
grid + jog pad), a center gcode preview, a right column (feed / rapid /
spindle overrides + spindle control), and a bottom gcode view. The
widgets wire themselves through `qtcnc_setup()`; this handler only logs
lifecycle and device events so an operator can see what the framework
is doing underneath the UI.
"""

from __future__ import annotations

from typing import Any

from qtcnc.core.types import ErrorSeverity, ProgramState, TaskMode
from qtcnc.handler import QtcncHandler


class StandardHandler(QtcncHandler):
    def on_startup(self) -> None:
        print(f"[qtcnc] standard screen startup: {self.config.window_title}")

    def on_ready(self) -> None:
        print("[qtcnc] standard screen ready")

    def on_estop(self, asserted: bool) -> None:
        print(f"[qtcnc] estop {'asserted' if asserted else 'cleared'}")

    def on_power(self, on: bool) -> None:
        print(f"[qtcnc] machine power {'on' if on else 'off'}")

    def on_mode_changed(self, mode: TaskMode) -> None:
        print(f"[qtcnc] mode -> {mode.name}")

    def on_file_loaded(self, path: str, program: ProgramState) -> None:
        print(f"[qtcnc] program loaded: {path} ({program.total_lines} lines)")

    def on_file_load_failed(self, path: str, reason: str) -> None:
        print(f"[qtcnc] program load failed: {path}: {reason}")

    def on_file_missing(self, path: str) -> None:
        print(f"[qtcnc] program missing: {path}")

    def on_file_closed(self) -> None:
        print("[qtcnc] program closed")

    def on_error(self, severity: ErrorSeverity, text: str) -> None:
        print(f"[qtcnc] error {severity.name}: {text}")

    def on_disconnected(self, reason: str) -> None:
        print(f"[qtcnc] disconnected: {reason}")

    def on_reconnected(self) -> None:
        print("[qtcnc] reconnected")

    def on_device_added(self, device: Any) -> None:
        print(f"[qtcnc] device added: {device}")

    def on_device_removed(self, device: Any) -> None:
        print(f"[qtcnc] device removed: {device}")

    def on_mount_added(self, mount: Any) -> None:
        print(f"[qtcnc] mount added: {mount.target} ({mount.fstype})")

    def on_mount_removed(self, mount: Any) -> None:
        print(f"[qtcnc] mount removed: {mount.target}")
