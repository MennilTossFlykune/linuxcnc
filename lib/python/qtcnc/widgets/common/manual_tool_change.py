"""ManualToolChangeDialog: HAL-triggered dialog for manual tool changes.

Replaces the Tcl ``hal_manualtoolchange`` component. The daemon's HAL
component owns the pins; ``iocontrol.0.tool-change`` / ``tool-changed``
/ ``tool-prep-number`` are netted to them via a POSTGUI_HALFILE.

Pins (declared under ``qtcnc.{name}.``)::

    change   BIT  IN   — rising edge shows the dialog
    changed  BIT  OUT  — set True on accept, False on reject
    number   S32  IN   — tool number requested by the interpreter

The handler wires this in two steps::

    def declare_pins(self):
        return ManualToolChangeDialog.declared_pins("manual-tool-change")

    def on_ready(self):
        self._mtc = ManualToolChangeDialog(self.window)
        self._mtc.hal_setup(self.ctx.hal, "manual-tool-change")
"""

from __future__ import annotations

from typing import Any

from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QDialogButtonBox, QLabel, QVBoxLayout

from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.widgets.common.dialog import QtcncDialog, Severity


class ManualToolChangeDialog(QtcncDialog):
    """Prompt the operator to insert a tool and press Continue."""

    HAL_PINS = [
        HalPinSpec(name="qtcnc.{name}.change", type=HalType.BIT, dir=HalDir.IN),
        HalPinSpec(name="qtcnc.{name}.changed", type=HalType.BIT, dir=HalDir.OUT),
        HalPinSpec(name="qtcnc.{name}.number", type=HalType.S32, dir=HalDir.IN),
    ]
    TRIGGER_PIN = "change"
    RESPONSE_PIN = "changed"

    def __init__(self, parent: Any = None) -> None:
        super().__init__(
            parent,
            title="Manual Tool Change",
            severity=Severity.WARNING,
            blocking=True,
            mandatory=True,
            buttons=QDialogButtonBox.StandardButton.Ok,
        )
        self.button(QDialogButtonBox.StandardButton.Ok).setText("Continue")

    def build_content(self, layout: QVBoxLayout) -> None:
        self._tool_label = QLabel(self)
        self._tool_label.setWordWrap(True)
        self._tool_label.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._tool_label)

    def on_trigger(self) -> None:
        # Defer to the next event-loop pass so pin updates from the
        # same daemon poll tick (number, change) all land first.
        QTimer.singleShot(0, self._show_tool_change)

    def _show_tool_change(self) -> None:
        if not self.trigger_high:
            return
        tool_num = 0
        try:
            tool_num = int(self.pin("number").value or 0)
        except (RuntimeError, KeyError, TypeError, ValueError):
            pass
        if tool_num > 0:
            self.header.setText(f"Manual Tool Change — T{tool_num}")
            self._tool_label.setText(
                f"Please insert tool T{tool_num} into the spindle\n"
                "and press Continue when ready."
            )
        else:
            self.header.setText("Manual Tool Change — Remove Tool")
            self._tool_label.setText(
                "Please remove the current tool from the spindle\n"
                "and press Continue when ready."
            )
        self.show()
