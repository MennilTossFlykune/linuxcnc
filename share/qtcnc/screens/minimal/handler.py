"""Minimal screen handler — the v1 demo.

Hooks just enough of the lifecycle to prove end-to-end wiring:

* Prints a one-liner on startup and ready.
* Logs estop transitions.
* Logs error messages from the daemon.
* Provides manual tool change via HAL pins.

The widgets in `main.ui` are wired automatically by the bootstrap
(widget `qtcnc_setup()` hooks + `auto_connect_handler()`), so this
handler does not need to connect any signals by hand.
"""

from __future__ import annotations

from qtcnc.core.hal_spec import HalPinSpec
from qtcnc.core.types import ErrorSeverity, TaskMode
from qtcnc.handler import QtcncHandler
from qtcnc.widgets.common.manual_tool_change import ManualToolChangeDialog


class MinimalHandler(QtcncHandler):
    def declare_pins(self) -> list[HalPinSpec]:
        return ManualToolChangeDialog.declared_pins("manual-tool-change")

    def on_startup(self) -> None:
        print(f"[qtcnc] startup: {self.config.window_title}")

    def on_ready(self) -> None:
        print("[qtcnc] ready")
        self._mtc = ManualToolChangeDialog(self.window)
        self._mtc.hal_setup(self.ctx.hal, "manual-tool-change")

    def on_estop(self, asserted: bool) -> None:
        print(f"[qtcnc] estop {'asserted' if asserted else 'cleared'}")

    def on_power(self, on: bool) -> None:
        print(f"[qtcnc] machine power {'on' if on else 'off'}")

    def on_mode_changed(self, mode: TaskMode) -> None:
        print(f"[qtcnc] mode -> {mode.name}")

    def on_error(self, severity: ErrorSeverity, text: str) -> None:
        print(f"[qtcnc] error {severity.name}: {text}")

    def on_disconnected(self, reason: str) -> None:
        print(f"[qtcnc] disconnected: {reason}")

    def on_reconnected(self) -> None:
        print("[qtcnc] reconnected")
