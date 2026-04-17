"""Standard screen handler — the v2 demo.

Layout exercise: every widget type the framework ships. The `main.ui`
drops a top bar (estop, power, state, file-open), a left column (DRO
grid + jog pad), a center gcode preview, a right column (feed / rapid /
spindle overrides + spindle control), and a bottom tabbed area (g-code
view + tool table). The widgets wire themselves through `qtcnc_setup()`;
this handler adds missing-tools detection on file load, manual tool
change via HAL pins, and logs lifecycle/device events.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtWidgets import QDialog

from qtcnc.core.hal_spec import HalPinSpec
from qtcnc.core.types import ErrorSeverity, ProgramState, TaskMode
from qtcnc.handler import QtcncHandler
from qtcnc.widgets.common.manual_tool_change import ManualToolChangeDialog
from qtcnc.widgets.common.missing_tools_dialog import MissingToolsDialog


class StandardHandler(QtcncHandler):
    def declare_pins(self) -> list[HalPinSpec]:
        return ManualToolChangeDialog.declared_pins("manual-tool-change")

    def on_startup(self) -> None:
        print(f"[qtcnc] standard screen startup: {self.config.window_title}")

    def on_ready(self) -> None:
        print("[qtcnc] standard screen ready")
        self._mtc = ManualToolChangeDialog(self.window)
        self._mtc.hal_setup(self.ctx.hal, "manual-tool-change")

    def on_estop(self, asserted: bool) -> None:
        print(f"[qtcnc] estop {'asserted' if asserted else 'cleared'}")

    def on_power(self, on: bool) -> None:
        print(f"[qtcnc] machine power {'on' if on else 'off'}")

    def on_mode_changed(self, mode: TaskMode) -> None:
        print(f"[qtcnc] mode -> {mode.name}")

    def on_file_loaded(self, path: str, program: ProgramState) -> None:
        print(f"[qtcnc] program loaded: {path} ({program.total_lines} lines)")
        self._check_missing_tools(path)

    def _check_missing_tools(self, path: str) -> None:
        try:
            from qtcnc.core.program import parse
        except ImportError:
            return
        try:
            parsed = parse(path, lenient=True)
        except Exception as e:
            print(f"[qtcnc] preview parse failed, skipping missing-tools check: {e}")
            return
        referenced = parsed.requested_tools
        if referenced:
            self.cmd.set_program_tools(referenced)
        else:
            return
        try:
            result = self.cmd.get_tool_db()
        except Exception:
            return
        known = {t.tool_id for t in result.tools}
        missing = referenced - known
        if not missing:
            return
        print(f"[qtcnc] {len(missing)} missing tool(s): {sorted(missing)}")
        dialog = MissingToolsDialog(missing, self.window)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            for tool_id, pocket in dialog.selected_tools():
                self.cmd.add_tool(tool_id, pocket)
            print(f"[qtcnc] added {len(dialog.selected_tools())} tool(s)")

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
