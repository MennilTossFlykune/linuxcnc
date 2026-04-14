"""SpindleControl: forward / reverse / stop buttons + speed spinbox."""

from __future__ import annotations

from typing import Any

from qtpy.QtCore import Property
from qtpy.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from qtcnc.core.types import SpindleDir
from qtcnc.widgets.base import QtcncWidget


class SpindleControl(QtcncWidget, QWidget):
    """Forward/Reverse/Stop buttons and a speed spinbox for one spindle.

    `index` is a Qt property so screens can drop multiple instances for
    multi-spindle machines. Speed is in machine units (RPM); the spinbox
    is the source of truth for "what would I command if I clicked CW now".
    External `spindle_speed_changed` updates rewrite the spinbox using
    the standard signal-block guard so we don't echo back.
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._index: int = 0
        self._direction: SpindleDir = SpindleDir.STOP

        self._cw_btn = QPushButton("CW", self)
        self._cw_btn.setObjectName("_spindle_cw")
        self._cw_btn.setCheckable(True)
        self._ccw_btn = QPushButton("CCW", self)
        self._ccw_btn.setObjectName("_spindle_ccw")
        self._ccw_btn.setCheckable(True)
        self._stop_btn = QPushButton("Stop", self)
        self._stop_btn.setObjectName("_spindle_stop")
        self._stop_btn.setCheckable(True)
        self._stop_btn.setChecked(True)

        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self._cw_btn)
        group.addButton(self._ccw_btn)
        group.addButton(self._stop_btn)

        self._speed_spin = QDoubleSpinBox(self)
        self._speed_spin.setRange(0.0, 100000.0)
        self._speed_spin.setDecimals(0)
        self._speed_spin.setSingleStep(100.0)
        self._speed_spin.setValue(0.0)
        self._speed_spin.setSuffix(" rpm")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(QLabel("Spindle:", self))
        layout.addWidget(self._cw_btn)
        layout.addWidget(self._ccw_btn)
        layout.addWidget(self._stop_btn)
        layout.addWidget(self._speed_spin, 1)

        self._cw_btn.clicked.connect(self._on_cw_clicked)
        self._ccw_btn.clicked.connect(self._on_ccw_clicked)
        self._stop_btn.clicked.connect(self._on_stop_clicked)

    # ----- Qt properties -----

    def _get_index(self) -> int:
        return self._index

    def _set_index(self, value: int) -> None:
        self._index = int(value)

    index = Property(int, _get_index, _set_index)

    # ----- qtcnc lifecycle -----

    def qtcnc_setup(self) -> None:
        self.connect_status("spindle_speed_changed", self._on_speed_changed)
        self.connect_status("spindle_direction_changed", self._on_direction_changed)
        spindles = self.window().qtcnc_status.state.spindles
        if self._index < len(spindles):
            sp = spindles[self._index]
            self._set_speed_silently(sp.speed)
            self._apply_direction(sp.direction)

    # ----- input handlers -----

    def _on_cw_clicked(self) -> None:
        cmd = self.window().qtcnc_command
        cmd.spindle_forward(self._speed_spin.value(), index=self._index)

    def _on_ccw_clicked(self) -> None:
        cmd = self.window().qtcnc_command
        cmd.spindle_reverse(self._speed_spin.value(), index=self._index)

    def _on_stop_clicked(self) -> None:
        cmd = self.window().qtcnc_command
        cmd.spindle_stop(index=self._index)

    # ----- external state updates -----

    def _on_speed_changed(self, index: int, value: float) -> None:
        if index != self._index:
            return
        self._set_speed_silently(float(value))

    def _on_direction_changed(self, index: int, direction: SpindleDir) -> None:
        if index != self._index:
            return
        self._apply_direction(direction)

    def _apply_direction(self, direction: SpindleDir) -> None:
        self._direction = direction
        if direction == SpindleDir.FORWARD:
            target = self._cw_btn
        elif direction == SpindleDir.REVERSE:
            target = self._ccw_btn
        else:
            target = self._stop_btn
        was_blocked = target.blockSignals(True)
        try:
            target.setChecked(True)
        finally:
            target.blockSignals(was_blocked)

    def _set_speed_silently(self, value: float) -> None:
        was_blocked = self._speed_spin.blockSignals(True)
        try:
            self._speed_spin.setValue(value)
        finally:
            self._speed_spin.blockSignals(was_blocked)
