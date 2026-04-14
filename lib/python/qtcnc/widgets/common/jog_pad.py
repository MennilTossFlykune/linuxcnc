"""JogPad: per-axis +/- jog buttons with continuous and incremental modes.

Layout: one row per axis (X, Y, Z, ...) with `<letter>-` and `<letter>+`
buttons, plus a small mode panel at the bottom holding a continuous /
incremental toggle and a velocity spinbox. Continuous jog presses fire
`command.jog_start(axis, signed_velocity)` on `pressed` and
`command.jog_stop(axis)` on `released`. Incremental jog issues a single
`command.jog_increment(axis, signed_distance, velocity)` per click.

The widget reads `axis_count` from the Status snapshot at qtcnc_setup
time, so it sizes itself to the running machine.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtCore import Property, Qt
from qtpy.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from qtcnc.widgets.base import QtcncWidget


_AXIS_LETTERS = ("X", "Y", "Z", "A", "B", "C", "U", "V", "W")


class JogPad(QtcncWidget, QWidget):
    """Per-axis jog buttons + continuous/incremental mode toggle."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._axis_count: int = 0
        self._velocity: float = 60.0
        self._increment: float = 1.0
        self._mode: str = "continuous"
        # Per-axis button references for tests and styling.
        self._minus_buttons: list[QPushButton] = []
        self._plus_buttons: list[QPushButton] = []
        self._velocity_spin: QDoubleSpinBox | None = None
        self._increment_spin: QDoubleSpinBox | None = None
        self._cont_radio: QRadioButton | None = None
        self._inc_radio: QRadioButton | None = None

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(4, 4, 4, 4)
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(4)
        self._outer.addLayout(self._grid)
        self._outer.addLayout(self._build_mode_panel())

    # ----- Qt properties -----

    def _get_velocity(self) -> float:
        return self._velocity

    def _set_velocity(self, value: float) -> None:
        self._velocity = float(value)
        if self._velocity_spin is not None:
            self._velocity_spin.setValue(self._velocity)

    velocity = Property(float, _get_velocity, _set_velocity)

    def _get_increment(self) -> float:
        return self._increment

    def _set_increment(self, value: float) -> None:
        self._increment = float(value)
        if self._increment_spin is not None:
            self._increment_spin.setValue(self._increment)

    increment = Property(float, _get_increment, _set_increment)

    # ----- qtcnc lifecycle -----

    def qtcnc_setup(self) -> None:
        machine = self.window().qtcnc_status.state.machine
        self._build_axis_rows(machine.axis_count)
        self.connect_status("estop_changed", self._on_estop_changed)
        self.connect_status("power_changed", self._on_power_changed)
        self._refresh_enabled()

    def _build_mode_panel(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._cont_radio = QRadioButton("Continuous", self)
        self._cont_radio.setChecked(True)
        self._inc_radio = QRadioButton("Increment", self)
        group = QButtonGroup(self)
        group.addButton(self._cont_radio)
        group.addButton(self._inc_radio)
        self._cont_radio.toggled.connect(self._on_mode_toggled)

        row.addWidget(self._cont_radio)
        row.addWidget(self._inc_radio)
        row.addSpacing(8)

        row.addWidget(QLabel("vel:", self))
        self._velocity_spin = QDoubleSpinBox(self)
        self._velocity_spin.setRange(0.0, 10000.0)
        self._velocity_spin.setDecimals(2)
        self._velocity_spin.setValue(self._velocity)
        self._velocity_spin.valueChanged.connect(self._on_velocity_changed)
        row.addWidget(self._velocity_spin)

        row.addSpacing(4)
        row.addWidget(QLabel("inc:", self))
        self._increment_spin = QDoubleSpinBox(self)
        self._increment_spin.setRange(0.0, 1000.0)
        self._increment_spin.setDecimals(4)
        self._increment_spin.setValue(self._increment)
        self._increment_spin.valueChanged.connect(self._on_increment_changed)
        row.addWidget(self._increment_spin)

        row.addStretch(1)
        return row

    def _build_axis_rows(self, axis_count: int) -> None:
        self._axis_count = max(0, min(axis_count, len(_AXIS_LETTERS)))
        for i in range(self._axis_count):
            letter = _AXIS_LETTERS[i]
            label = QLabel(letter, self)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            minus = QPushButton(f"{letter}-", self)
            minus.setObjectName(f"_jog_{letter.lower()}_minus")
            minus.setAutoRepeat(False)
            plus = QPushButton(f"{letter}+", self)
            plus.setObjectName(f"_jog_{letter.lower()}_plus")
            plus.setAutoRepeat(False)
            self._grid.addWidget(label, i, 0)
            self._grid.addWidget(minus, i, 1)
            self._grid.addWidget(plus, i, 2)
            self._minus_buttons.append(minus)
            self._plus_buttons.append(plus)
            # Capture i by default-arg so each closure binds the right axis.
            minus.pressed.connect(lambda i=i: self._on_pressed(i, -1))
            minus.released.connect(lambda i=i: self._on_released(i))
            plus.pressed.connect(lambda i=i: self._on_pressed(i, +1))
            plus.released.connect(lambda i=i: self._on_released(i))

    # ----- input handlers -----

    def _on_pressed(self, axis: int, sign: int) -> None:
        cmd = self.window().qtcnc_command
        if self._mode == "continuous":
            cmd.jog_start(axis, sign * self._velocity)
        # In increment mode the actual move fires on release so a quick
        # tap behaves like "tap to step", matching most existing CNC UIs.

    def _on_released(self, axis: int) -> None:
        cmd = self.window().qtcnc_command
        if self._mode == "continuous":
            cmd.jog_stop(axis)
            return
        # Incremental: button-up is the trigger so we know the user
        # actually wanted the step (rather than starting and aborting).
        sender = self.sender()
        sign = +1
        if sender in self._minus_buttons:
            sign = -1
        cmd.jog_increment(axis, sign * self._increment, self._velocity)

    def _on_mode_toggled(self, checked: bool) -> None:
        self._mode = "continuous" if checked else "increment"

    def _on_velocity_changed(self, value: float) -> None:
        self._velocity = float(value)

    def _on_increment_changed(self, value: float) -> None:
        self._increment = float(value)

    # ----- enable/disable based on machine state -----

    def _on_estop_changed(self, asserted: bool) -> None:
        self._refresh_enabled()

    def _on_power_changed(self, on: bool) -> None:
        self._refresh_enabled()

    def _refresh_enabled(self) -> None:
        machine = self.window().qtcnc_status.state.machine
        active = (not machine.estop) and machine.powered
        for btn in self._minus_buttons + self._plus_buttons:
            btn.setEnabled(active)
