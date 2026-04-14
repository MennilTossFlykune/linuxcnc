"""DroGrid: multi-axis DRO laid out as a grid of axis-letter / value rows.

Where `DroWidget` shows one axis, `DroGrid` shows every axis the machine
exposes. Axis count is read from `status.state.machine.axis_count` at
qtcnc_setup time, so the grid sizes itself to the running machine.

`coord_system` is a Qt property so screens can pick "work" (default),
"machine", or "dtg" in Designer. The grid subscribes to the matching
umbrella signal:

* "work"    -> position_changed(Position)
* "machine" -> machine_position_changed(Position)
* "dtg"     -> dtg_changed(Position)

Each axis gets one HAL FLOAT OUT pin named `qtcnc.<grid>.<letter>-out`.
The pin set is computed dynamically per instance via `declared_pins`,
because the class-level template alone can't enumerate axes.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtCore import Property
from qtpy.QtWidgets import QGridLayout, QLabel, QWidget

from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.types import Position
from qtcnc.widgets.base import QtcncWidget


_AXIS_LETTERS = ("X", "Y", "Z", "A", "B", "C", "U", "V", "W")
_AXIS_FIELDS = ("x", "y", "z", "a", "b", "c", "u", "v", "w")


_COORD_SIGNALS: dict[str, str] = {
    "work": "position_changed",
    "machine": "machine_position_changed",
    "dtg": "dtg_changed",
}


class DroGrid(QtcncWidget, QWidget):
    """Multi-axis position display, one row per axis."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._coord_system: str = "work"
        self._format: str = "{value:+9.4f}"
        self._axis_count: int = 0
        self._value_labels: list[QLabel] = []
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(2)

    # ----- Qt properties -----

    def _get_coord_system(self) -> str:
        return self._coord_system

    def _set_coord_system(self, value: str) -> None:
        if value not in _COORD_SIGNALS:
            raise ValueError(
                f"coord_system must be one of {tuple(_COORD_SIGNALS)}, got {value!r}"
            )
        self._coord_system = value

    coord_system = Property(str, _get_coord_system, _set_coord_system)

    def _get_format(self) -> str:
        return self._format

    def _set_format(self, value: str) -> None:
        self._format = value
        self._refresh_all()

    format_string = Property(str, _get_format, _set_format)

    # ----- pin declaration -----

    @classmethod
    def declared_pins(cls, instance: "QtcncWidget") -> list[HalPinSpec]:
        # Class-level HAL_PINS doesn't apply: the pin set depends on the
        # axis count, which is only known after Status has a snapshot.
        # An instance reads its machine config here so bootstrap can ask
        # for the right pins before DECLARE_PINS goes out.
        name = instance.objectName() or ""
        if not name:
            raise ValueError(
                "DroGrid has no objectName(); set one in Designer or via "
                "setObjectName() before bootstrap"
            )
        axis_count = _resolve_axis_count(instance)
        pins: list[HalPinSpec] = []
        for i in range(axis_count):
            letter = _AXIS_LETTERS[i].lower()
            pins.append(HalPinSpec(
                name=f"qtcnc.{name}.{letter}-out",
                type=HalType.FLOAT,
                dir=HalDir.OUT,
                owner_widget=name,
            ))
        return pins

    # ----- qtcnc lifecycle -----

    def qtcnc_setup(self) -> None:
        machine = self.window().qtcnc_status.state.machine
        self._build_rows(machine.axis_count)
        signal_name = _COORD_SIGNALS[self._coord_system]
        self.connect_status(signal_name, self._on_position_changed)
        # Seed from current snapshot.
        snapshot_pos = self._read_snapshot_position()
        if snapshot_pos is not None:
            self._on_position_changed(snapshot_pos)

    def _build_rows(self, axis_count: int) -> None:
        self._axis_count = max(0, min(axis_count, len(_AXIS_LETTERS)))
        for i in range(self._axis_count):
            letter_label = QLabel(_AXIS_LETTERS[i], self)
            letter_label.setObjectName(f"_dro_letter_{_AXIS_LETTERS[i].lower()}")
            value_label = QLabel(self._format.format(value=0.0), self)
            value_label.setObjectName(f"_dro_value_{_AXIS_LETTERS[i].lower()}")
            self._grid.addWidget(letter_label, i, 0)
            self._grid.addWidget(value_label, i, 1)
            self._value_labels.append(value_label)

    def _on_position_changed(self, pos: Position) -> None:
        for i in range(self._axis_count):
            field = _AXIS_FIELDS[i]
            value = getattr(pos, field, None)
            if value is None:
                continue
            self._set_axis_value(i, float(value))

    def _set_axis_value(self, axis: int, value: float) -> None:
        if axis < 0 or axis >= len(self._value_labels):
            return
        self._value_labels[axis].setText(self._format.format(value=value))
        try:
            letter = _AXIS_LETTERS[axis].lower()
            self.pin(f"{letter}-out").set(value)
        except KeyError:
            pass

    def _refresh_all(self) -> None:
        for i, lbl in enumerate(self._value_labels):
            current = lbl.text()
            try:
                old_value = float(current.strip())
            except ValueError:
                old_value = 0.0
            lbl.setText(self._format.format(value=old_value))

    def _read_snapshot_position(self) -> Position | None:
        state = self.window().qtcnc_status.state
        if self._coord_system == "machine":
            return state.machine_position
        if self._coord_system == "dtg":
            return state.dtg
        return state.position


def _resolve_axis_count(widget: QtcncWidget) -> int:
    """Best-effort lookup of the active axis count for `widget`.

    DroGrid runs declared_pins() at bootstrap time, before qtcnc_setup,
    so we can't yet rely on `connect_status`. Read the count from the
    Status snapshot if a window is attached; fall back to 3.
    """
    try:
        win = widget.window()  # type: ignore[attr-defined]
    except RuntimeError:
        return 3
    if win is None:
        return 3
    status = getattr(win, "qtcnc_status", None)
    if status is None:
        return 3
    machine = status.state.machine
    return max(1, min(int(machine.axis_count), len(_AXIS_LETTERS)))
