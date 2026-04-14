"""MachinePowerButton: toggles machine power; disabled while estopped."""

from __future__ import annotations

from typing import Any

from qtpy.QtWidgets import QPushButton

from qtcnc.widgets.base import QtcncWidget


class MachinePowerButton(QtcncWidget, QPushButton):
    """Push button bound to `status.power_changed` and `status.estop_changed`.

    * Disabled (grayed out) while the machine is estopped.
    * Label shows `POWER ON` / `POWER OFF` depending on the current state.
    * Click routes to `cmd.power_on()` / `cmd.power_off()`.
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._estopped: bool = True
        self._powered: bool = False
        self._refresh()
        self.clicked.connect(self._on_clicked)

    def qtcnc_setup(self) -> None:
        self.connect_status("estop_changed", self._on_estop_changed)
        self.connect_status("power_changed", self._on_power_changed)
        machine = self.window().qtcnc_status.state.machine
        self._estopped = machine.estop
        self._powered = machine.powered
        self._refresh()

    def _on_estop_changed(self, asserted: bool) -> None:
        self._estopped = asserted
        self._refresh()

    def _on_power_changed(self, on: bool) -> None:
        self._powered = on
        self._refresh()

    def _refresh(self) -> None:
        self.setEnabled(not self._estopped)
        self.setText("POWER OFF" if self._powered else "POWER ON")

    def _on_clicked(self) -> None:
        cmd = self.window().qtcnc_command
        if self._powered:
            cmd.power_off()
        else:
            cmd.power_on()
