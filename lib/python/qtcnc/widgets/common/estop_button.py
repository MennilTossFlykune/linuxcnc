"""EstopButton: toggles machine estop via the Command facade."""

from __future__ import annotations

from typing import Any

from qtpy.QtWidgets import QPushButton

from qtcnc.widgets.base import QtcncWidget


class EstopButton(QtcncWidget, QPushButton):
    """Push button bound to `status.estop_changed` and `command.estop*`.

    * Label shows `ESTOP`/`ESTOP RESET` depending on current state.
    * Click: if currently estopped, calls `cmd.estop_reset()`; otherwise
      calls `cmd.estop()`.
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._estopped: bool = True
        self._refresh_label()
        self.clicked.connect(self._on_clicked)

    def qtcnc_setup(self) -> None:
        self.connect_status("estop_changed", self._on_estop_changed)
        # Seed from current state so the label is correct at show time.
        self._on_estop_changed(self.window().qtcnc_status.state.machine.estop)

    def _on_estop_changed(self, asserted: bool) -> None:
        self._estopped = asserted
        self._refresh_label()

    def _refresh_label(self) -> None:
        self.setText("ESTOP RESET" if self._estopped else "ESTOP")

    def _on_clicked(self) -> None:
        cmd = self.window().qtcnc_command
        if self._estopped:
            cmd.estop_reset()
        else:
            cmd.estop()
