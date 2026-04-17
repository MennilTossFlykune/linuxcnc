"""StateLabel: one label widget for every piece of displayable state.

Set the ``state`` Qt property to select what is shown; auxiliary
properties (``axis``, ``index``, ``format_string``) parametrize the
chosen state. One widget class covers everything a screen might want
to read off ``Status.state``:

Machine-level text
    * ``task_state``     — ESTOP / ESTOP RESET / OFF / ON
    * ``task_mode``      — MANUAL / AUTO / MDI
    * ``interp_state``   — IDLE / READING / PAUSED / WAITING
    * ``motion_type``    — NONE / TRAVERSE / FEED / ARC / TOOLCHANGE / PROBING / INDEX ROTARY
    * ``motion_mode``    — FREE / COORD / TELEOP (trajectory-planner mode)
    * ``linear_units``   — MM / INCH
    * ``g5x_index``      — G54 / G55 / G56 / G57 / G58 / G59 / G59.1 / G59.2 / G59.3

Per-axis DROs
    * ``dro_work``       — work-coord axis reading (uses ``axis`` 0..8)
    * ``dro_machine``    — absolute-machine axis reading
    * ``dro_dtg``        — distance-to-go axis reading

Program
    * ``program_line``   — current g-code line number
    * ``program_path``   — loaded program path (basename)

Spindle (uses ``index`` 0..7)
    * ``spindle_speed``     — commanded RPM
    * ``spindle_direction`` — FORWARD / REVERSE / STOP

Tool / feed / rapid
    * ``tool_in_spindle``   — current T number
    * ``feed_rate``         — current path feed, units/min
    * ``rapid_rate``        — current rapid rate, units/min
    * ``feed_override``     — % of programmed feed
    * ``rapid_override``    — % of programmed rapid
    * ``spindle_override``  — % of programmed spindle speed (``index``)

Each spec declares the ``Status`` signals it listens to and a ``text``
callback that formats the label from the live ``StateStore``. The
lifecycle mirrors :class:`ActionButton`: ``qtcnc_setup`` connects the
spec's signals, and any received signal triggers ``_refresh``, which
reads the current ``StateStore`` and updates the displayed text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable

from qtpy.QtCore import Property
from qtpy.QtWidgets import QLabel

from qtcnc.core.state import StateStore
from qtcnc.core.types import (
    InterpState,
    LinearUnits,
    MotionMode,
    MotionType,
    SpindleDir,
    TaskMode,
    TaskState,
)
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


_AXIS_LETTERS = ("X", "Y", "Z", "A", "B", "C", "U", "V", "W")
_AXIS_FIELDS = ("x", "y", "z", "a", "b", "c", "u", "v", "w")


_TASK_STATE_TEXT: dict[TaskState, str] = {
    TaskState.ESTOP: "ESTOPPED",
    TaskState.ESTOP_RESET: "ESTOP RESET",
    TaskState.OFF: "OFF",
    TaskState.ON: "ON",
}

_TASK_MODE_TEXT: dict[TaskMode, str] = {
    TaskMode.MANUAL: "MANUAL",
    TaskMode.AUTO: "AUTO",
    TaskMode.MDI: "MDI",
}

_INTERP_STATE_TEXT: dict[InterpState, str] = {
    InterpState.IDLE: "IDLE",
    InterpState.READING: "READING",
    InterpState.PAUSED: "PAUSED",
    InterpState.WAITING: "WAITING",
}

_MOTION_TYPE_TEXT: dict[MotionType, str] = {
    MotionType.NONE: "NONE",
    MotionType.TRAVERSE: "TRAVERSE",
    MotionType.FEED: "FEED",
    MotionType.ARC: "ARC",
    MotionType.TOOLCHANGE: "TOOLCHANGE",
    MotionType.PROBING: "PROBING",
    MotionType.INDEX_ROTARY: "INDEX ROTARY",
}

_MOTION_MODE_TEXT: dict[MotionMode, str] = {
    MotionMode.FREE: "FREE",
    MotionMode.COORD: "COORD",
    MotionMode.TELEOP: "TELEOP",
}

_LINEAR_UNITS_TEXT: dict[LinearUnits, str] = {
    LinearUnits.MM: "MM",
    LinearUnits.INCH: "INCH",
}

_SPINDLE_DIR_TEXT: dict[SpindleDir, str] = {
    SpindleDir.FORWARD: "FORWARD",
    SpindleDir.REVERSE: "REVERSE",
    SpindleDir.STOP: "STOP",
}

# G5x index 1..9 maps to the canonical work-coord name.
_G5X_NAMES: tuple[str, ...] = (
    "G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3",
)


_UNIT_DRO_FORMATS: dict[LinearUnits, str] = {
    LinearUnits.MM: "{letter}: {value:+9.3f}",
    LinearUnits.INCH: "{letter}: {value:+9.4f}",
}


# ---------------------------------------------------------------------------
# State spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StateSpec:
    """Describes how a StateLabel shows one slice of ``StateStore``."""

    signals: tuple[str, ...]
    text: Callable[["StateLabel", StateStore], str]


def _axis_index(label: "StateLabel") -> int:
    idx = label._axis
    if 0 <= idx < len(_AXIS_LETTERS):
        return idx
    return 0


def _axis_letter(label: "StateLabel") -> str:
    return _AXIS_LETTERS[_axis_index(label)]


def _dro_format(label: "StateLabel", state: StateStore) -> str:
    if label._format:
        return label._format
    return _UNIT_DRO_FORMATS.get(
        state.machine.linear_units, "{letter}: {value:+9.3f}"
    )


def _position_value(pos: Any, index: int) -> float:
    field = _AXIS_FIELDS[index]
    value = getattr(pos, field, None)
    if value is None:
        return 0.0
    return float(value)


# ----- machine-level text -----


def _text_task_state(label: "StateLabel", state: StateStore) -> str:
    return _TASK_STATE_TEXT.get(state.task_state, str(state.task_state))


def _text_task_mode(label: "StateLabel", state: StateStore) -> str:
    return _TASK_MODE_TEXT.get(state.machine.task_mode, str(state.machine.task_mode))


def _text_interp_state(label: "StateLabel", state: StateStore) -> str:
    return _INTERP_STATE_TEXT.get(
        state.machine.interp_state, str(state.machine.interp_state)
    )


def _text_motion_type(label: "StateLabel", state: StateStore) -> str:
    return _MOTION_TYPE_TEXT.get(
        state.machine.motion_type, str(state.machine.motion_type)
    )


def _text_motion_mode(label: "StateLabel", state: StateStore) -> str:
    return _MOTION_MODE_TEXT.get(
        state.machine.motion_mode, str(state.machine.motion_mode)
    )


def _text_linear_units(label: "StateLabel", state: StateStore) -> str:
    return _LINEAR_UNITS_TEXT.get(
        state.machine.linear_units, str(state.machine.linear_units)
    )


def _text_g5x_index(label: "StateLabel", state: StateStore) -> str:
    idx = int(state.offsets.g5x_index)
    if 1 <= idx <= len(_G5X_NAMES):
        return _G5X_NAMES[idx - 1]
    return f"G5x?{idx}"


# ----- per-axis DROs -----


def _text_dro_work(label: "StateLabel", state: StateStore) -> str:
    idx = _axis_index(label)
    fmt = _dro_format(label, state)
    return fmt.format(letter=_AXIS_LETTERS[idx], value=_position_value(state.position, idx))


def _text_dro_machine(label: "StateLabel", state: StateStore) -> str:
    idx = _axis_index(label)
    fmt = _dro_format(label, state)
    return fmt.format(
        letter=_AXIS_LETTERS[idx], value=_position_value(state.machine_position, idx)
    )


def _text_dro_dtg(label: "StateLabel", state: StateStore) -> str:
    idx = _axis_index(label)
    fmt = _dro_format(label, state)
    return fmt.format(letter=_AXIS_LETTERS[idx], value=_position_value(state.dtg, idx))


# ----- program -----


def _text_program_line(label: "StateLabel", state: StateStore) -> str:
    return str(int(state.program.current_line))


def _text_program_path(label: "StateLabel", state: StateStore) -> str:
    path = state.program.path
    if not path:
        return ""
    return path.rsplit("/", 1)[-1]


# ----- spindle -----


def _spindle_index(label: "StateLabel") -> int:
    return max(0, int(label._index))


def _text_spindle_speed(label: "StateLabel", state: StateStore) -> str:
    idx = _spindle_index(label)
    if idx >= len(state.spindles):
        return "0"
    fmt = label._format or "{value:.0f}"
    return fmt.format(value=float(state.spindles[idx].speed))


def _text_spindle_direction(label: "StateLabel", state: StateStore) -> str:
    idx = _spindle_index(label)
    if idx >= len(state.spindles):
        return _SPINDLE_DIR_TEXT[SpindleDir.STOP]
    direction = state.spindles[idx].direction
    return _SPINDLE_DIR_TEXT.get(direction, str(direction))


# ----- tool / feed / rapid / overrides -----


def _text_tool_in_spindle(label: "StateLabel", state: StateStore) -> str:
    return f"T{int(state.tool_in_spindle)}"


def _text_feed_rate(label: "StateLabel", state: StateStore) -> str:
    fmt = label._format or "{value:.1f}"
    return fmt.format(value=float(state.feed_rate))


def _text_rapid_rate(label: "StateLabel", state: StateStore) -> str:
    fmt = label._format or "{value:.1f}"
    return fmt.format(value=float(state.rapid_rate))


def _text_feed_override(label: "StateLabel", state: StateStore) -> str:
    fmt = label._format or "{value:.0f}%"
    return fmt.format(value=float(state.overrides.feed) * 100.0)


def _text_rapid_override(label: "StateLabel", state: StateStore) -> str:
    fmt = label._format or "{value:.0f}%"
    return fmt.format(value=float(state.overrides.rapid) * 100.0)


def _text_spindle_override(label: "StateLabel", state: StateStore) -> str:
    idx = _spindle_index(label)
    if idx >= len(state.overrides.spindles):
        return "100%"
    fmt = label._format or "{value:.0f}%"
    return fmt.format(value=float(state.overrides.spindles[idx]) * 100.0)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_STATE_SPECS: dict[str, _StateSpec] = {
    "task_state": _StateSpec(
        signals=("task_state_changed",),
        text=_text_task_state,
    ),
    "task_mode": _StateSpec(
        signals=("task_mode_changed",),
        text=_text_task_mode,
    ),
    "interp_state": _StateSpec(
        signals=("interp_state_changed",),
        text=_text_interp_state,
    ),
    "motion_type": _StateSpec(
        signals=("motion_type_changed",),
        text=_text_motion_type,
    ),
    "motion_mode": _StateSpec(
        signals=("machine_state_changed",),
        text=_text_motion_mode,
    ),
    "linear_units": _StateSpec(
        signals=("machine_state_changed",),
        text=_text_linear_units,
    ),
    "g5x_index": _StateSpec(
        signals=("offsets_changed",),
        text=_text_g5x_index,
    ),
    "dro_work": _StateSpec(
        signals=("position_changed", "machine_state_changed"),
        text=_text_dro_work,
    ),
    "dro_machine": _StateSpec(
        signals=("machine_position_changed", "machine_state_changed"),
        text=_text_dro_machine,
    ),
    "dro_dtg": _StateSpec(
        signals=("dtg_changed", "machine_state_changed"),
        text=_text_dro_dtg,
    ),
    "program_line": _StateSpec(
        signals=("program_line_changed", "program_loaded", "program_closed"),
        text=_text_program_line,
    ),
    "program_path": _StateSpec(
        signals=("program_loaded", "program_closed"),
        text=_text_program_path,
    ),
    "spindle_speed": _StateSpec(
        signals=("spindle_speed_changed", "spindles_changed"),
        text=_text_spindle_speed,
    ),
    "spindle_direction": _StateSpec(
        signals=("spindle_direction_changed", "spindles_changed"),
        text=_text_spindle_direction,
    ),
    "tool_in_spindle": _StateSpec(
        signals=("tool_in_spindle_changed",),
        text=_text_tool_in_spindle,
    ),
    "feed_rate": _StateSpec(
        signals=("feed_rate_changed",),
        text=_text_feed_rate,
    ),
    "rapid_rate": _StateSpec(
        signals=("rapid_rate_changed",),
        text=_text_rapid_rate,
    ),
    "feed_override": _StateSpec(
        signals=("feed_override_changed", "overrides_changed"),
        text=_text_feed_override,
    ),
    "rapid_override": _StateSpec(
        signals=("rapid_override_changed", "overrides_changed"),
        text=_text_rapid_override,
    ),
    "spindle_override": _StateSpec(
        signals=("spindle_override_changed", "overrides_changed"),
        text=_text_spindle_override,
    ),
}


_VALID_STATES: tuple[str, ...] = tuple(_STATE_SPECS.keys())

StateKind = _register_enum(
    IntEnum("StateKind", [(name, i) for i, name in enumerate(_VALID_STATES)]),
)


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class StateLabel(QtcncWidget, QLabel):
    """One label widget for every piece of displayable state.

    Default ``state`` is ``task_state`` so a StateLabel dropped into a
    screen without a ``state`` property still shows something useful.
    """

    StateKind = StateKind
    if _Q_ENUMS is not None:
        _Q_ENUMS(StateKind)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._state: StateKind = StateKind.task_state
        self._axis: int = 0
        self._index: int = 0
        self._format: str = ""

    # ----- spec access -----

    def _spec(self) -> _StateSpec:
        return _STATE_SPECS[self._state.name]

    # ----- Qt properties -----

    def _get_state(self) -> "StateKind":
        return self._state

    def _set_state(self, value: Any) -> None:
        if isinstance(value, StateKind):
            kind = value
        elif isinstance(value, str):
            try:
                kind = StateKind[value]
            except KeyError:
                raise ValueError(
                    f"state must be one of {_VALID_STATES}, got {value!r}"
                )
        else:
            try:
                kind = StateKind(int(value))
            except (ValueError, TypeError):
                raise ValueError(
                    f"state must be one of {_VALID_STATES}, got {value!r}"
                )
        self._state = kind

    state = Property(StateKind, _get_state, _set_state)

    def _get_axis(self) -> int:
        return self._axis

    def _set_axis(self, value: int) -> None:
        self._axis = int(value)

    axis = Property(int, _get_axis, _set_axis)

    def _get_index(self) -> int:
        return self._index

    def _set_index(self, value: int) -> None:
        self._index = int(value)

    index = Property(int, _get_index, _set_index)

    def _get_format(self) -> str:
        return self._format

    def _set_format(self, value: str) -> None:
        self._format = str(value)

    format_string = Property(str, _get_format, _set_format)

    # ----- qtcnc lifecycle -----

    def qtcnc_setup(self) -> None:
        spec = self._spec()
        for signal_name in spec.signals:
            self.connect_status(signal_name, self._on_state_change)
        self._refresh()

    def _on_state_change(self, *args: Any) -> None:
        self._refresh()

    def _refresh(self) -> None:
        spec = self._spec()
        state = self.window().qtcnc_status.state
        self.setText(spec.text(self, state))
