"""OverrideSlider: a feed/rapid/spindle override slider with a value label.

The widget is bound to one of three Status signals depending on its
`kind` Qt property:

* "feed"    -> feed_override_changed(float)  / command.feedrate(scale)
* "rapid"   -> rapid_override_changed(float) / command.rapidrate(scale)
* "spindle" -> spindle_override_changed(int, float) / command.spindleoverride(scale, index)

The slider talks to the Command facade in fractional units (1.0 == 100%)
matching the LinuxCNC convention. Internally it scales by `_RESOLUTION`
to give the QSlider an integer range without losing precision.

Feedback-loop guard: when an external state update arrives, the slider's
`valueChanged` signal is blocked while we set the new position so the
widget doesn't echo the value back to the daemon as a fresh command.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from qtpy.QtCore import Property, Qt
from qtpy.QtWidgets import QHBoxLayout, QLabel, QSlider, QWidget

from qtcnc.widgets.base import QtcncWidget

try:
    from qtpy.QtCore import pyqtEnum as _pyqt_enum
except ImportError:
    _pyqt_enum = None

try:
    from qtpy.QtCore import Q_ENUMS as _Q_ENUMS
except ImportError:
    _Q_ENUMS = None


def _register_enum(cls):
    if _pyqt_enum is not None:
        return _pyqt_enum(cls)
    return cls


_KINDS: tuple[str, ...] = ("feed", "rapid", "spindle")
_RESOLUTION = 1000  # 1.0 fractional override == 1000 slider ticks

_KIND_TO_SIGNAL: dict[str, str] = {
    "feed": "feed_override_changed",
    "rapid": "rapid_override_changed",
    "spindle": "spindle_override_changed",
}

OverrideKind = _register_enum(
    IntEnum("OverrideKind", [(name, i) for i, name in enumerate(_KINDS)]),
)


class OverrideSlider(QtcncWidget, QWidget):
    """Slider + percentage label for one of the override channels."""

    OverrideKind = OverrideKind
    if _Q_ENUMS is not None:
        _Q_ENUMS(OverrideKind)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._kind: OverrideKind = OverrideKind.feed
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

    def _get_kind(self) -> "OverrideKind":
        return self._kind

    def _set_kind(self, value: Any) -> None:
        if isinstance(value, OverrideKind):
            kind = value
        elif isinstance(value, str):
            try:
                kind = OverrideKind[value]
            except KeyError:
                raise ValueError(f"kind must be one of {_KINDS}, got {value!r}")
        else:
            try:
                kind = OverrideKind(int(value))
            except (ValueError, TypeError):
                raise ValueError(f"kind must be one of {_KINDS}, got {value!r}")
        self._kind = kind

    kind = Property(OverrideKind, _get_kind, _set_kind)

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
        self._apply_config_max()
        signal_name = _KIND_TO_SIGNAL[self._kind.name]
        if self._kind is OverrideKind.spindle:
            self.connect_status(signal_name, self._on_spindle_changed)
            initial = self._read_initial_spindle()
        else:
            self.connect_status(signal_name, self._on_external_changed)
            initial = self._read_initial_simple()
        self._set_value_silently(initial)

    def _apply_config_max(self) -> None:
        config = getattr(self.window(), "qtcnc_config", None)
        if config is None:
            return
        if self._kind is OverrideKind.feed:
            self._set_max(config.max_feed_override)
        elif self._kind is OverrideKind.rapid:
            self._set_max(config.max_rapid_override)
        else:
            self._set_max(config.max_spindle_override)

    def _read_initial_simple(self) -> float:
        overrides = self.window().qtcnc_status.state.overrides
        if self._kind is OverrideKind.feed:
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
        if self._kind is OverrideKind.feed:
            cmd.feedrate(value)
        elif self._kind is OverrideKind.rapid:
            cmd.rapidrate(value)
        else:
            cmd.spindleoverride(value, index=self._index)

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
