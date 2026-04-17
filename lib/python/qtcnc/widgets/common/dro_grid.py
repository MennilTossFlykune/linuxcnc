"""DroGrid: multi-axis DRO laid out as a grid of axis-letter / value rows.

One row is created per Cartesian axis the machine exposes, as reported by
`machine.axis_mask`. Gantry configurations like `X Y Y Z` (four joints,
three axes) get three rows — one per unique letter — and a straight
`XYZ` machine gets three rows too, correctly.

`coord_system` is a Qt property so screens can pick "work" (default),
"machine", or "dtg" in Designer. The grid subscribes to the matching
umbrella signal:

* "work"    -> position_changed(Position)
* "machine" -> machine_position_changed(Position)
* "dtg"     -> dtg_changed(Position)

The display format adapts to `machine.linear_units`: metric shows three
decimal places (0.001 mm — 1 micron, the standard manufacturing
resolution) and imperial shows four (0.0001"). Screens that want to
override this can set the `format_string` Qt property in Designer; a
non-empty value wins and the unit-driven auto-format is ignored.

Each axis gets one HAL FLOAT OUT pin named `qtcnc.<grid>.<letter>-out`.
The pin set is computed dynamically per instance via `declared_pins`,
because the class-level template alone can't enumerate axes.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtCore import Property
from qtpy.QtWidgets import QGridLayout, QLabel, QWidget

from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.types import LinearUnits, MachineState, Position
from qtcnc.widgets.base import QtcncWidget


_AXIS_LETTERS = ("X", "Y", "Z", "A", "B", "C", "U", "V", "W")
_AXIS_FIELDS = ("x", "y", "z", "a", "b", "c", "u", "v", "w")


_COORD_SIGNALS: dict[str, str] = {
    "work": "position_changed",
    "machine": "machine_position_changed",
    "dtg": "dtg_changed",
}


_UNIT_FORMATS: dict[LinearUnits, str] = {
    LinearUnits.MM: "{value:9.3f}",
    LinearUnits.INCH: "{value:9.4f}",
}


def _mask_indices(mask: int) -> tuple[int, ...]:
    return tuple(i for i in range(len(_AXIS_LETTERS)) if mask & (1 << i))


class DroGrid(QtcncWidget, QWidget):
    """Multi-axis position display, one row per Cartesian axis."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._coord_system: str = "work"
        self._explicit_format: str = ""
        self._linear_units: LinearUnits = LinearUnits.MM
        self._axis_indices: tuple[int, ...] = ()
        self._value_labels: list[QLabel] = []
        self._last_values: dict[int, float] = {}
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
        return self._explicit_format

    def _set_format(self, value: str) -> None:
        self._explicit_format = value
        self._refresh_all()

    format_string = Property(str, _get_format, _set_format)

    # ----- pin declaration -----

    @classmethod
    def declared_pins(cls, instance: "QtcncWidget") -> list[HalPinSpec]:
        # Class-level HAL_PINS can't parameterize over the machine axis
        # set; pins are per-axis and the axis set is only knowable after
        # the Status snapshot lands.
        name = instance.objectName() or ""
        if not name:
            raise ValueError(
                "DroGrid has no objectName(); set one in Designer or via "
                "setObjectName() before bootstrap"
            )
        mask = _resolve_axis_mask(instance)
        pins: list[HalPinSpec] = []
        for i in _mask_indices(mask):
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
        self._linear_units = machine.linear_units
        self._build_rows(machine.axis_mask)
        signal_name = _COORD_SIGNALS[self._coord_system]
        self.connect_status(signal_name, self._on_position_changed)
        self.connect_status("machine_state_changed", self._on_machine_changed)
        snapshot_pos = self._read_snapshot_position()
        if snapshot_pos is not None:
            self._on_position_changed(snapshot_pos)

    def _build_rows(self, axis_mask: int) -> None:
        self._axis_indices = _mask_indices(axis_mask)
        fmt = self._current_format()
        for row, i in enumerate(self._axis_indices):
            letter_label = QLabel(_AXIS_LETTERS[i], self)
            letter_label.setObjectName(f"_dro_letter_{_AXIS_LETTERS[i].lower()}")
            value_label = QLabel(fmt.format(value=0.0), self)
            value_label.setObjectName(f"_dro_value_{_AXIS_LETTERS[i].lower()}")
            self._grid.addWidget(letter_label, row, 0)
            self._grid.addWidget(value_label, row, 1)
            self._value_labels.append(value_label)
            self._last_values[i] = 0.0

    def _on_position_changed(self, pos: Position) -> None:
        for row, i in enumerate(self._axis_indices):
            field = _AXIS_FIELDS[i]
            value = getattr(pos, field, None)
            if value is None:
                continue
            self._set_axis_value(row, i, float(value))

    def _on_machine_changed(self, machine: MachineState) -> None:
        if machine.linear_units != self._linear_units:
            self._linear_units = machine.linear_units
            self._refresh_all()

    def _set_axis_value(self, row: int, axis_index: int, value: float) -> None:
        if row < 0 or row >= len(self._value_labels):
            return
        self._last_values[axis_index] = value
        self._value_labels[row].setText(self._current_format().format(value=value))
        try:
            letter = _AXIS_LETTERS[axis_index].lower()
            self.pin(f"{letter}-out").set(value)
        except KeyError:
            pass

    def _refresh_all(self) -> None:
        fmt = self._current_format()
        for row, i in enumerate(self._axis_indices):
            if row >= len(self._value_labels):
                break
            self._value_labels[row].setText(
                fmt.format(value=self._last_values.get(i, 0.0)),
            )

    def _current_format(self) -> str:
        if self._explicit_format:
            return self._explicit_format
        return _UNIT_FORMATS.get(self._linear_units, "{value:9.3f}")

    def _read_snapshot_position(self) -> Position | None:
        state = self.window().qtcnc_status.state
        if self._coord_system == "machine":
            return state.machine_position
        if self._coord_system == "dtg":
            return state.dtg
        return state.position


def _resolve_axis_mask(widget: QtcncWidget) -> int:
    """Best-effort lookup of the active axis mask for `widget`.

    `declared_pins()` fires at bootstrap, before `qtcnc_setup`, so
    `connect_status` isn't available yet. Read the mask from the current
    Status snapshot if a window is attached; fall back to XYZ (0b111).
    """
    try:
        win = widget.window()  # type: ignore[attr-defined]
    except RuntimeError:
        return 0b111
    if win is None:
        return 0b111
    status = getattr(win, "qtcnc_status", None)
    if status is None:
        return 0b111
    machine = status.state.machine
    mask = int(machine.axis_mask) & ((1 << len(_AXIS_LETTERS)) - 1)
    return mask or 0b111
