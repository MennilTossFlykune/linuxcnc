"""DroWidget: single-axis digital readout label.

A DroWidget shows the current value of one position axis. It reacts to
`status.axis_position_changed(axis, value)` and only updates when the
axis index matches its own configured axis.

It also declares a FLOAT output HAL pin so external HAL nets can read
the displayed value. The pin is updated every time the displayed value
changes.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtCore import Property
from qtpy.QtWidgets import QLabel

from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.widgets.base import QtcncWidget


_AXIS_LETTERS = ("X", "Y", "Z", "A", "B", "C", "U", "V", "W")


class DroWidget(QtcncWidget, QLabel):
    """Label reading one axis from Status; publishes a HAL pin."""

    HAL_PINS = [
        HalPinSpec(name="qtcnc.{name}.value-out", type=HalType.FLOAT, dir=HalDir.OUT),
    ]

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._axis: int = 0
        self._value: float = 0.0
        self._format: str = "{letter}: {value:+8.4f}"
        self._refresh()

    # ----- Qt properties for Designer -----

    def _get_axis(self) -> int:
        return self._axis

    def _set_axis(self, value: int) -> None:
        if 0 <= value < len(_AXIS_LETTERS):
            self._axis = int(value)
            self._refresh()

    axis = Property(int, _get_axis, _set_axis)

    def _get_format(self) -> str:
        return self._format

    def _set_format(self, value: str) -> None:
        self._format = value
        self._refresh()

    format_string = Property(str, _get_format, _set_format)

    # ----- qtcnc lifecycle -----

    def qtcnc_setup(self) -> None:
        self.connect_status("axis_position_changed", self._on_axis_changed)
        # Seed from current Status state.
        pos = self.window().qtcnc_status.state.position
        letter = _AXIS_LETTERS[self._axis].lower()
        current = getattr(pos, letter, None)
        if current is not None:
            self._apply_value(float(current))

    def _on_axis_changed(self, axis: int, value: float) -> None:
        if axis == self._axis:
            self._apply_value(value)

    def _apply_value(self, value: float) -> None:
        self._value = value
        self._refresh()
        # Push the displayed value to the HAL pin so external nets can read it.
        try:
            self.pin("value-out").set(value)
        except KeyError:
            # The hub may not have registered our pin yet in tests; skip silently.
            pass

    def _refresh(self) -> None:
        letter = _AXIS_LETTERS[self._axis]
        self.setText(self._format.format(letter=letter, value=self._value))
