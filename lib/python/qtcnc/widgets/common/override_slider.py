"""OverrideSlider: a feed/rapid/spindle override slider with a value label.

The widget is bound to one of three Status signals depending on its
`kind` Qt property:

* "feed"    -> feed_override_changed(float)  / command.set_feed_override(value)
* "rapid"   -> rapid_override_changed(float) / command.set_rapid_override(value)
* "spindle" -> spindle_override_changed(int, float) / command.set_spindle_override(value, index)

The slider talks to the Command facade in fractional units (1.0 == 100%)
matching the LinuxCNC convention. Internally it scales by `_RESOLUTION`
to give the QSlider an integer range without losing precision.

Feedback-loop guard: when an external state update arrives, the slider's
`valueChanged` signal is blocked while we set the new position so the
widget doesn't echo the value back to the daemon as a fresh command.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtCore import Property, Qt
from qtpy.QtWidgets import QHBoxLayout, QLabel, QSlider, QWidget

from qtcnc.widgets.base import QtcncWidget


_KINDS = ("feed", "rapid", "spindle")
_RESOLUTION = 1000  # 1.0 fractional override == 1000 slider ticks

_KIND_TO_SIGNAL: dict[str, str] = {
    "feed": "feed_override_changed",
    "rapid": "rapid_override_changed",
    "spindle": "spindle_override_changed",
}


class OverrideSlider(QtcncWidget, QWidget):
    """Slider + percentage label for one of the override channels."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._kind: str = "feed"
        self._index: int = 0
        self._max_value: float = 1.5  # 150% by default; matches LinuxCNC sim
        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(0, int(self._max_value * _RESOLUTION))
        self._slider.setValue(_RESOLUTION)
        self._label = QLabel("100%", self)
        self._label.setMinimumWidth(48)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._label)
        self._slider.valueChanged.connect(self._on_user_changed)

    # ----- Qt properties -----

    def _get_kind(self) -> str:
        return self._kind

    def _set_kind(self, value: str) -> None:
        if value not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}, got {value!r}")
        self._kind = value

    kind = Property(str, _get_kind, _set_kind)

    def _get_index(self) -> int:
        return self._index

    def _set_index(self, value: int) -> None:
        self._index = int(value)

    index = Property(int, _get_index, _set_index)

    def _get_max(self) -> float:
        return self._max_value

    def _set_max(self, value: float) -> None:
        self._max_value = max(0.01, float(value))
        self._slider.setRange(0, int(self._max_value * _RESOLUTION))

    max_value = Property(float, _get_max, _set_max)

    # ----- qtcnc lifecycle -----

    def qtcnc_setup(self) -> None:
        signal_name = _KIND_TO_SIGNAL[self._kind]
        if self._kind == "spindle":
            self.connect_status(signal_name, self._on_spindle_changed)
            initial = self._read_initial_spindle()
        else:
            self.connect_status(signal_name, self._on_external_changed)
            initial = self._read_initial_simple()
        self._set_value_silently(initial)

    def _read_initial_simple(self) -> float:
        overrides = self.window().qtcnc_status.state.overrides
        if self._kind == "feed":
            return overrides.feed
        return overrides.rapid

    def _read_initial_spindle(self) -> float:
        overrides = self.window().qtcnc_status.state.overrides
        if self._index < len(overrides.spindles):
            return overrides.spindles[self._index]
        return 1.0

    # ----- input handlers -----

    def _on_user_changed(self, raw: int) -> None:
        value = raw / _RESOLUTION
        self._refresh_label(value)
        cmd = self.window().qtcnc_command
        if self._kind == "feed":
            cmd.set_feed_override(value)
        elif self._kind == "rapid":
            cmd.set_rapid_override(value)
        else:
            cmd.set_spindle_override(value, index=self._index)

    def _on_external_changed(self, value: float) -> None:
        self._set_value_silently(float(value))

    def _on_spindle_changed(self, index: int, value: float) -> None:
        if index != self._index:
            return
        self._set_value_silently(float(value))

    def _set_value_silently(self, value: float) -> None:
        was_blocked = self._slider.blockSignals(True)
        try:
            self._slider.setValue(int(round(value * _RESOLUTION)))
        finally:
            self._slider.blockSignals(was_blocked)
        self._refresh_label(value)

    def _refresh_label(self, value: float) -> None:
        self._label.setText(f"{int(round(value * 100))}%")
