"""ActionButton: one button widget for every Command action.

Set the ``action`` Qt property to select a behavior; additional
properties (``axis``, ``target``, ``direction``, ``velocity``,
``distance``, ``speed``, ``index``, ``mdi_line``, ``path``) parametrize
the chosen action. Four behavior styles are covered by the same widget:

* ``toggle`` — ``estop``, ``power``, ``mist_toggle``, ``flood_toggle``,
  ``home_toggle``. The click chooses the opposite verb from the live
  machine state; the label is always a reflection of the live state.
  Checked flag is driven by state when meaningful (estop, power,
  coolant); ``home_toggle`` uses label-only feedback since it performs
  both home and unhome.
* ``radio`` — ``mode`` (manual/auto/mdi) and ``spindle``
  (forward/reverse/stop). Checkable. Several buttons share the same
  action and use the ``target`` property to select one exclusive
  sub-state; the checked flag is driven off the matching state field.
* ``momentary`` — ``jog``. Not checkable. Press dispatches
  ``jog_continuous``; release dispatches ``jog_stop``. The dispatch is
  keyed off mouse events so a physical press maps to exactly one
  cycle, regardless of where the cursor moves.
* ``trigger`` — ``home``, ``unhome``, ``jog_increment``,
  ``auto_run``, ``auto_pause``, ``auto_resume``, ``abort``,
  ``auto_step``, ``mdi``, ``mdi_entry``,
  ``load_program``, ``mist``, ``flood``. Not checkable. One click,
  one dispatch. ``mdi_entry`` forwards its click to the ``execute()``
  method of the sibling MdiEntry widget named by ``target``.

On click, ``nextCheckState`` flips the checked flag to its expected
post-dispatch value (toggles invert, radios assert themselves). Qt
runs ``nextCheckState`` inside its ``blockRefresh`` window, so the
single post-click repaint already has the new value — no frame
where the button shows unchecked before the clicked handler runs.
``_on_clicked`` then dispatches the command. The daemon applies it
and the resulting state diff flows back through ``Status`` signals,
triggering ``_refresh`` to confirm or correct the visual.

Enable gating reuses ``_VERB_TO_PREDICATE`` from ``qtcnc.core.command``
so widgets and daemon share one source of truth for "can this verb
run right now". Stop-spindle, power-off, and estop are never gated
so operators always have a working escape hatch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable

from qtpy.QtCore import Property, Qt, Signal
from qtpy.QtWidgets import QDialogButtonBox, QFileDialog, QPushButton

from qtcnc.core.command import Command, _VERB_TO_PREDICATE
from qtcnc.core.state import StateStore
from qtcnc.core.types import MachineState, MotionMode, SpindleDir, SpindleState, TaskMode
from qtcnc.signals import CommandVerb
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


_GCODE_FILTER = "G-code files (*.ngc *.nc *.tap *.gcode);;All files (*)"

_SPINDLE_TARGETS = ("forward", "reverse", "stop")
_MODE_TARGETS = ("manual", "auto", "mdi")
_ONOFF_TARGETS = ("on", "off")
_TRAJ_TARGETS = ("free", "coord", "teleop")

_MODE_ENUM: dict[str, TaskMode] = {
    "manual": TaskMode.MANUAL,
    "auto": TaskMode.AUTO,
    "mdi": TaskMode.MDI,
}

_SPINDLE_ENUM: dict[str, SpindleDir] = {
    "forward": SpindleDir.FORWARD,
    "reverse": SpindleDir.REVERSE,
    "stop": SpindleDir.STOP,
}

_TRAJ_ENUM: dict[str, MotionMode] = {
    "free": MotionMode.FREE,
    "coord": MotionMode.COORD,
    "teleop": MotionMode.TELEOP,
}


# ---------------------------------------------------------------------------
# Action spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ActionSpec:
    style: str
    signals: tuple[str, ...]
    dispatch: Callable[["ActionButton", Command], None]
    release: Callable[["ActionButton", Command], None] | None
    checked: Callable[["ActionButton", StateStore], bool] | None
    enabled: Callable[["ActionButton", StateStore], bool]
    label: Callable[["ActionButton", StateStore], str | None] | None
    relevant_props: tuple[str, ...] = ()


def _verb_enabled(verb: CommandVerb) -> Callable[["ActionButton", StateStore], bool]:
    """Build an enable-predicate that forwards to the shared verb gate."""
    entry = _VERB_TO_PREDICATE.get(verb)
    if entry is None:
        return lambda b, s: True
    predicate, _ = entry
    return lambda b, s, _p=predicate: _p(s)


def _always_enabled(btn: "ActionButton", state: StateStore) -> bool:
    return True


# ----- estop -----

def _estop_dispatch(btn: "ActionButton", cmd: Command) -> None:
    if btn.window().qtcnc_status.state.machine.estop:
        cmd.state_estop_reset()
    else:
        cmd.state_estop()


def _estop_checked(btn: "ActionButton", state: StateStore) -> bool:
    return state.machine.estop


def _estop_label(btn: "ActionButton", state: StateStore) -> str:
    return "ESTOP RESET" if state.machine.estop else "ESTOP"


# ----- power -----

def _power_dispatch(btn: "ActionButton", cmd: Command) -> None:
    if btn.window().qtcnc_status.state.machine.powered:
        cmd.state_off()
    else:
        cmd.state_on()


def _power_checked(btn: "ActionButton", state: StateStore) -> bool:
    return state.machine.powered


def _power_enabled(btn: "ActionButton", state: StateStore) -> bool:
    if state.machine.powered:
        return True
    return not state.machine.estop


def _power_label(btn: "ActionButton", state: StateStore) -> str:
    return "POWER OFF" if state.machine.powered else "POWER ON"


# ----- mode -----

def _mode_target(btn: "ActionButton") -> TaskMode | None:
    target = btn.target.lower()
    return _MODE_ENUM.get(target)


def _require_mode_target(btn: "ActionButton") -> TaskMode:
    mode = _mode_target(btn)
    if mode is None:
        raise ValueError(
            f"action=mode requires target in {_MODE_TARGETS}, got {btn.target!r}"
        )
    return mode


def _mode_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.mode(_require_mode_target(btn))


def _mode_checked(btn: "ActionButton", state: StateStore) -> bool:
    mode = _mode_target(btn)
    if mode is None:
        return False
    return state.machine.task_mode == mode


def _mode_enabled(btn: "ActionButton", state: StateStore) -> bool:
    entry = _VERB_TO_PREDICATE[CommandVerb.SET_MODE]
    return entry[0](state)


# ----- spindle -----

def _spindle_target(btn: "ActionButton") -> str | None:
    target = btn.target.lower()
    return target if target in _SPINDLE_ENUM else None


def _require_spindle_target(btn: "ActionButton") -> str:
    target = _spindle_target(btn)
    if target is None:
        raise ValueError(
            f"action=spindle requires target in {_SPINDLE_TARGETS}, got {btn.target!r}"
        )
    return target


def _spindle_dispatch(btn: "ActionButton", cmd: Command) -> None:
    target = _require_spindle_target(btn)
    if target == "forward":
        cmd.spindle_forward(btn.speed, index=btn.index)
    elif target == "reverse":
        cmd.spindle_reverse(btn.speed, index=btn.index)
    else:
        cmd.spindle_off(index=btn.index)


def _spindle_checked(btn: "ActionButton", state: StateStore) -> bool:
    target = _spindle_target(btn)
    if target is None:
        return False
    target_enum = _SPINDLE_ENUM[target]
    if btn.index >= len(state.spindles):
        return target_enum == SpindleDir.STOP
    return state.spindles[btn.index].direction == target_enum


def _spindle_enabled(btn: "ActionButton", state: StateStore) -> bool:
    if _spindle_target(btn) == "stop":
        return True
    return state.machine.can_spindle


# ----- jog -----

def _jog_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.jog_continuous(btn.axis, btn.direction * btn.velocity, joint=btn.joint)


def _jog_release(btn: "ActionButton", cmd: Command) -> None:
    cmd.jog_stop(btn.axis, joint=btn.joint)


def _jog_enabled(btn: "ActionButton", state: StateStore) -> bool:
    return state.machine.can_jog


# ----- jog_increment -----

def _jog_inc_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.jog_increment(btn.axis, btn.direction * btn.distance, btn.velocity, joint=btn.joint)


# ----- home / unhome -----

def _home_dispatch(btn: "ActionButton", cmd: Command) -> None:
    joint = btn.joint if btn.joint >= 0 else btn.axis
    cmd.home(joint if joint >= 0 else -1)


def _unhome_dispatch(btn: "ActionButton", cmd: Command) -> None:
    joint = btn.joint if btn.joint >= 0 else btn.axis
    cmd.unhome(joint)


def _home_target_joint(btn: "ActionButton") -> int:
    """Resolve the joint this home/unhome button targets. -1 means all."""
    j = btn.joint if btn.joint >= 0 else btn.axis
    return j if j >= 0 else -1


def _joint_is_homing(state: StateStore, joint: int) -> bool:
    """True if the specified joint (or any joint when -1) is mid-homing."""
    if joint < 0:
        return state.is_any_homing
    if joint < len(state.joints):
        return state.joints[joint].is_homing
    return False


def _home_enabled(btn: "ActionButton", state: StateStore) -> bool:
    if not state.machine.can_home:
        return False
    return not _joint_is_homing(state, _home_target_joint(btn))


def _unhome_enabled(btn: "ActionButton", state: StateStore) -> bool:
    if not state.machine.can_unhome:
        return False
    return not _joint_is_homing(state, _home_target_joint(btn))


# ----- program_* -----

def _program_signals() -> tuple[str, ...]:
    return (
        "machine_state_changed",
        "program_started",
        "program_paused",
        "program_finished",
        "program_loaded",
        "program_tools_changed",
        "tool_table_changed",
    )


# ----- mdi -----

def _mdi_dispatch(btn: "ActionButton", cmd: Command) -> None:
    if not btn.mdi_line:
        return
    cmd.mdi(btn.mdi_line)


def _mdi_entry_dispatch(btn: "ActionButton", cmd: Command) -> None:
    name = btn.target
    if not name:
        return
    from qtpy.QtWidgets import QWidget
    entry = btn.window().findChild(QWidget, name)
    if entry is None:
        return
    execute = getattr(entry, "execute", None)
    if execute is None:
        return
    execute()


# ----- load_program -----

def _load_program_dispatch(btn: "ActionButton", cmd: Command) -> None:
    path = btn.path
    if not path:
        picked, _ = QFileDialog.getOpenFileName(
            btn, "Open G-code program", btn._last_dir, _GCODE_FILTER,
        )
        if not picked:
            return
        btn._last_dir = os.path.dirname(picked)
        path = picked
    cmd.load_program(path)


def _load_program_enabled(btn: "ActionButton", state: StateStore) -> bool:
    return state.can_load_program


# ----- mist / flood -----

def _require_onoff_target(btn: "ActionButton") -> str:
    target = btn.target.lower()
    if target not in _ONOFF_TARGETS:
        raise ValueError(
            f"action={btn._action.name} requires target in {_ONOFF_TARGETS}, "
            f"got {btn.target!r}"
        )
    return target


def _mist_dispatch(btn: "ActionButton", cmd: Command) -> None:
    if _require_onoff_target(btn) == "on":
        cmd.mist_on()
    else:
        cmd.mist_off()


def _flood_dispatch(btn: "ActionButton", cmd: Command) -> None:
    if _require_onoff_target(btn) == "on":
        cmd.flood_on()
    else:
        cmd.flood_off()


def _coolant_enabled(btn: "ActionButton", state: StateStore) -> bool:
    return state.machine.is_ready


# ----- mist_toggle / flood_toggle -----

def _mist_toggle_dispatch(btn: "ActionButton", cmd: Command) -> None:
    if btn.window().qtcnc_status.state.coolant.mist:
        cmd.mist_off()
    else:
        cmd.mist_on()


def _mist_toggle_checked(btn: "ActionButton", state: StateStore) -> bool:
    return state.coolant.mist


def _mist_toggle_label(btn: "ActionButton", state: StateStore) -> str:
    return "MIST OFF" if state.coolant.mist else "MIST ON"


def _flood_toggle_dispatch(btn: "ActionButton", cmd: Command) -> None:
    if btn.window().qtcnc_status.state.coolant.flood:
        cmd.flood_off()
    else:
        cmd.flood_on()


def _flood_toggle_checked(btn: "ActionButton", state: StateStore) -> bool:
    return state.coolant.flood


def _flood_toggle_label(btn: "ActionButton", state: StateStore) -> str:
    return "FLOOD OFF" if state.coolant.flood else "FLOOD ON"


# ----- home_toggle -----

def _home_toggle_axis(btn: "ActionButton") -> int:
    if btn.joint >= 0:
        return btn.window().qtcnc_status.state.machine.axis_for_joint(btn.joint)
    return btn.axis


def _home_toggle_joints(btn: "ActionButton", machine: MachineState) -> tuple[int, ...]:
    if btn.joint >= 0:
        return (btn.joint,)
    if btn.axis < 0:
        return ()
    return machine.joints_for_axis(btn.axis)


def _axis_label(btn: "ActionButton", machine: MachineState) -> str:
    axis = _home_toggle_axis(btn)
    letters = machine.axis_letters
    if 0 <= axis < len(letters):
        return letters[axis]
    return str(axis)


def _show_confirm_dialog(btn: "ActionButton", label: str) -> bool:
    from qtcnc.widgets.common.dialog import QtcncDialog, Severity

    dlg = QtcncDialog(
        btn.window(),
        title="Confirm Unhome",
        message=f"Unhome {label}?",
        severity=Severity.WARNING,
        buttons=(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        ),
    )
    return dlg.exec() == QtcncDialog.DialogCode.Accepted


def _home_toggle_dispatch(btn: "ActionButton", cmd: Command) -> None:
    state = btn.window().qtcnc_status.state
    machine = state.machine
    homed = machine.homed
    joints = _home_toggle_joints(btn, machine)
    if not joints:
        if machine.is_all_homed:
            if btn.confirm and not _show_confirm_dialog(btn, "all axes"):
                return
            for j in range(len(homed)):
                cmd.unhome(j)
        else:
            cmd.home(-1)
    elif all(homed[j] for j in joints if j < len(homed)):
        if btn.confirm and not _show_confirm_dialog(btn, _axis_label(btn, machine)):
            return
        for j in joints:
            cmd.unhome(j)
    else:
        for j in joints:
            cmd.home(j)


def _home_toggle_label(btn: "ActionButton", state: StateStore) -> str:
    machine = state.machine
    homed = machine.homed
    joints = _home_toggle_joints(btn, machine)
    if not joints:
        if state.is_any_homing:
            return "HOMING..."
        return "UNHOME ALL" if machine.is_all_homed else "HOME ALL"
    targeted_homing = any(
        state.joints[j].is_homing
        for j in joints if j < len(state.joints)
    )
    if targeted_homing:
        return f"HOMING {_axis_label(btn, machine)}..."
    if all(homed[j] for j in joints if j < len(homed)):
        return f"UNHOME {_axis_label(btn, machine)}"
    return f"HOME {_axis_label(btn, machine)}"


def _home_toggle_enabled(btn: "ActionButton", state: StateStore) -> bool:
    if not state.machine.can_home:
        return False
    joints = _home_toggle_joints(btn, state.machine)
    if not joints:
        return not state.is_any_homing
    return not any(
        state.joints[j].is_homing
        for j in joints if j < len(state.joints)
    )


# ----- traj_mode (radio) -----

def _traj_mode_target(btn: "ActionButton") -> MotionMode | None:
    return _TRAJ_ENUM.get(btn.target.lower())


def _require_traj_mode_target(btn: "ActionButton") -> MotionMode:
    mode = _traj_mode_target(btn)
    if mode is None:
        raise ValueError(
            f"action=traj_mode requires target in {_TRAJ_TARGETS}, got {btn.target!r}"
        )
    return mode


def _traj_mode_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.traj_mode(_require_traj_mode_target(btn))


def _traj_mode_checked(btn: "ActionButton", state: StateStore) -> bool:
    mode = _traj_mode_target(btn)
    if mode is None:
        return False
    return state.machine.motion_mode == mode


def _traj_mode_enabled(btn: "ActionButton", state: StateStore) -> bool:
    entry = _VERB_TO_PREDICATE[CommandVerb.TRAJ_MODE]
    return entry[0](state)


# ----- brake (toggle) -----

def _brake_spindle(btn: "ActionButton", state: StateStore) -> SpindleState | None:
    if btn.index >= len(state.spindles):
        return None
    return state.spindles[btn.index]


def _brake_dispatch(btn: "ActionButton", cmd: Command) -> None:
    spindle = _brake_spindle(btn, btn.window().qtcnc_status.state)
    if spindle is not None and spindle.brake:
        cmd.brake_release(index=btn.index)
    else:
        cmd.brake_engage(index=btn.index)


def _brake_checked(btn: "ActionButton", state: StateStore) -> bool:
    spindle = _brake_spindle(btn, state)
    return bool(spindle and spindle.brake)


def _brake_label(btn: "ActionButton", state: StateStore) -> str:
    spindle = _brake_spindle(btn, state)
    return "BRAKE RELEASE" if spindle and spindle.brake else "BRAKE ENGAGE"


def _brake_enabled(btn: "ActionButton", state: StateStore) -> bool:
    return state.machine.is_ready


# ----- task_info toggles -----

def _optional_stop_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.set_optional_stop(not btn.window().qtcnc_status.state.task_info.optional_stop)


def _optional_stop_checked(btn: "ActionButton", state: StateStore) -> bool:
    return state.task_info.optional_stop


def _block_delete_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.set_block_delete(not btn.window().qtcnc_status.state.task_info.block_delete)


def _block_delete_checked(btn: "ActionButton", state: StateStore) -> bool:
    return state.task_info.block_delete


# ----- override-enable toggles -----

def _feed_override_enabled_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.set_feed_override(
        not btn.window().qtcnc_status.state.overrides.feed_enabled
    )


def _feed_override_enabled_checked(btn: "ActionButton", state: StateStore) -> bool:
    return state.overrides.feed_enabled


def _spindle_override_enabled_dispatch(btn: "ActionButton", cmd: Command) -> None:
    state = btn.window().qtcnc_status.state
    current = True
    if btn.index < len(state.spindles):
        current = state.spindles[btn.index].override_enabled
    cmd.set_spindle_override(not current, index=btn.index)


def _spindle_override_enabled_checked(btn: "ActionButton", state: StateStore) -> bool:
    if btn.index >= len(state.spindles):
        return True
    return state.spindles[btn.index].override_enabled


def _feed_hold_enabled_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.set_feed_hold(
        not btn.window().qtcnc_status.state.overrides.hold_enabled
    )


def _feed_hold_enabled_checked(btn: "ActionButton", state: StateStore) -> bool:
    return state.overrides.hold_enabled


def _adaptive_feed_enabled_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.set_adaptive_feed(
        not btn.window().qtcnc_status.state.overrides.adaptive_enabled
    )


def _adaptive_feed_enabled_checked(btn: "ActionButton", state: StateStore) -> bool:
    return state.overrides.adaptive_enabled


# ----- parametrized triggers -----

def _set_max_velocity_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.maxvel(btn.value)


def _set_tool_offset_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.tool_offset(btn.index, zoffset=btn.value)


def _set_min_limit_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.set_min_limit(btn.axis, btn.value)


def _set_max_limit_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.set_max_limit(btn.axis, btn.value)


def _set_digital_output_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.set_digital_output(btn.index, _require_onoff_target(btn) == "on")


def _set_analog_output_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.set_analog_output(btn.index, btn.value)


def _send_error_msg_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.error_msg(btn.message)


def _send_text_msg_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.text_msg(btn.message)


def _send_display_msg_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.display_msg(btn.message)


def _set_debug_dispatch(btn: "ActionButton", cmd: Command) -> None:
    cmd.debug(btn.mask)


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------


_ACTION_SPECS: dict[str, _ActionSpec] = {
    "estop": _ActionSpec(
        style="toggle",
        signals=("estop_changed", "machine_state_changed"),
        dispatch=_estop_dispatch,
        release=None,
        checked=_estop_checked,
        enabled=_always_enabled,
        label=_estop_label,
    ),
    "power": _ActionSpec(
        style="toggle",
        signals=("estop_changed", "power_changed", "machine_state_changed"),
        dispatch=_power_dispatch,
        release=None,
        checked=_power_checked,
        enabled=_power_enabled,
        label=_power_label,
    ),
    "mode": _ActionSpec(
        style="radio",
        signals=("task_mode_changed", "machine_state_changed"),
        dispatch=_mode_dispatch,
        release=None,
        checked=_mode_checked,
        enabled=_mode_enabled,
        label=None,
        relevant_props=("target",),
    ),
    "spindle": _ActionSpec(
        style="radio",
        signals=("spindle_direction_changed", "machine_state_changed"),
        dispatch=_spindle_dispatch,
        release=None,
        checked=_spindle_checked,
        enabled=_spindle_enabled,
        label=None,
        relevant_props=("target", "speed", "index"),
    ),
    "jog": _ActionSpec(
        style="momentary",
        signals=("machine_state_changed",),
        dispatch=_jog_dispatch,
        release=_jog_release,
        checked=None,
        enabled=_jog_enabled,
        label=None,
        relevant_props=("axis", "joint", "direction", "velocity"),
    ),
    "jog_increment": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_jog_inc_dispatch,
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.JOG_INCREMENT),
        label=None,
        relevant_props=("axis", "joint", "direction", "distance", "velocity"),
    ),
    "home": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed", "any_homing_changed"),
        dispatch=_home_dispatch,
        release=None,
        checked=None,
        enabled=_home_enabled,
        label=None,
        relevant_props=("axis", "joint"),
    ),
    "unhome": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed", "any_homing_changed"),
        dispatch=_unhome_dispatch,
        release=None,
        checked=None,
        enabled=_unhome_enabled,
        label=None,
        relevant_props=("axis", "joint"),
    ),
    "auto_run": _ActionSpec(
        style="trigger",
        signals=_program_signals(),
        dispatch=lambda b, c: c.auto_run(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.AUTO_RUN),
        label=None,
    ),
    "auto_pause": _ActionSpec(
        style="trigger",
        signals=_program_signals(),
        dispatch=lambda b, c: c.auto_pause(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.AUTO_PAUSE),
        label=None,
    ),
    "auto_resume": _ActionSpec(
        style="trigger",
        signals=_program_signals(),
        dispatch=lambda b, c: c.auto_resume(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.AUTO_RESUME),
        label=None,
    ),
    "abort": _ActionSpec(
        style="trigger",
        signals=_program_signals(),
        dispatch=lambda b, c: c.abort(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.ABORT),
        label=None,
    ),
    "auto_step": _ActionSpec(
        style="trigger",
        signals=_program_signals(),
        dispatch=lambda b, c: c.auto_step(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.AUTO_STEP),
        label=None,
    ),
    "mdi": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_mdi_dispatch,
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.MDI),
        label=None,
        relevant_props=("mdi_line",),
    ),
    "mdi_entry": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_mdi_entry_dispatch,
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.MDI),
        label=None,
        relevant_props=("target",),
    ),
    "load_program": _ActionSpec(
        style="trigger",
        signals=_program_signals(),
        dispatch=_load_program_dispatch,
        release=None,
        checked=None,
        enabled=_load_program_enabled,
        label=None,
        relevant_props=("path",),
    ),
    "mist": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_mist_dispatch,
        release=None,
        checked=None,
        enabled=_coolant_enabled,
        label=None,
        relevant_props=("target",),
    ),
    "flood": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_flood_dispatch,
        release=None,
        checked=None,
        enabled=_coolant_enabled,
        label=None,
        relevant_props=("target",),
    ),
    "mist_toggle": _ActionSpec(
        style="toggle",
        signals=("coolant_changed", "machine_state_changed"),
        dispatch=_mist_toggle_dispatch,
        release=None,
        checked=_mist_toggle_checked,
        enabled=_coolant_enabled,
        label=_mist_toggle_label,
    ),
    "flood_toggle": _ActionSpec(
        style="toggle",
        signals=("coolant_changed", "machine_state_changed"),
        dispatch=_flood_toggle_dispatch,
        release=None,
        checked=_flood_toggle_checked,
        enabled=_coolant_enabled,
        label=_flood_toggle_label,
    ),
    "home_toggle": _ActionSpec(
        style="toggle",
        signals=("homed_changed", "all_homed_changed", "any_homing_changed",
                 "machine_state_changed"),
        dispatch=_home_toggle_dispatch,
        release=None,
        checked=None,
        enabled=_home_toggle_enabled,
        label=_home_toggle_label,
        relevant_props=("axis", "joint", "confirm"),
    ),
    "traj_mode": _ActionSpec(
        style="radio",
        signals=("machine_state_changed",),
        dispatch=_traj_mode_dispatch,
        release=None,
        checked=_traj_mode_checked,
        enabled=_traj_mode_enabled,
        label=None,
        relevant_props=("target",),
    ),
    "brake": _ActionSpec(
        style="toggle",
        signals=("spindles_changed", "machine_state_changed"),
        dispatch=_brake_dispatch,
        release=None,
        checked=_brake_checked,
        enabled=_brake_enabled,
        label=_brake_label,
        relevant_props=("index",),
    ),
    "optional_stop": _ActionSpec(
        style="toggle",
        signals=("task_info_changed",),
        dispatch=_optional_stop_dispatch,
        release=None,
        checked=_optional_stop_checked,
        enabled=_always_enabled,
        label=None,
    ),
    "block_delete": _ActionSpec(
        style="toggle",
        signals=("task_info_changed",),
        dispatch=_block_delete_dispatch,
        release=None,
        checked=_block_delete_checked,
        enabled=_always_enabled,
        label=None,
    ),
    "feed_override_enabled": _ActionSpec(
        style="toggle",
        signals=("overrides_changed",),
        dispatch=_feed_override_enabled_dispatch,
        release=None,
        checked=_feed_override_enabled_checked,
        enabled=_always_enabled,
        label=None,
    ),
    "spindle_override_enabled": _ActionSpec(
        style="toggle",
        signals=("spindles_changed",),
        dispatch=_spindle_override_enabled_dispatch,
        release=None,
        checked=_spindle_override_enabled_checked,
        enabled=_always_enabled,
        label=None,
        relevant_props=("index",),
    ),
    "feed_hold_enabled": _ActionSpec(
        style="toggle",
        signals=("overrides_changed",),
        dispatch=_feed_hold_enabled_dispatch,
        release=None,
        checked=_feed_hold_enabled_checked,
        enabled=_always_enabled,
        label=None,
    ),
    "adaptive_feed_enabled": _ActionSpec(
        style="toggle",
        signals=("overrides_changed",),
        dispatch=_adaptive_feed_enabled_dispatch,
        release=None,
        checked=_adaptive_feed_enabled_checked,
        enabled=_always_enabled,
        label=None,
    ),
    "maxvel": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_set_max_velocity_dispatch,
        release=None,
        checked=None,
        enabled=_always_enabled,
        label=None,
        relevant_props=("value",),
    ),
    "tool_offset": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_set_tool_offset_dispatch,
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.TOOL_OFFSET),
        label=None,
        relevant_props=("index", "value"),
    ),
    "load_tool_table": _ActionSpec(
        style="trigger",
        signals=_program_signals(),
        dispatch=lambda b, c: c.load_tool_table(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.LOAD_TOOL_TABLE),
        label=None,
    ),
    "task_plan_synch": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=lambda b, c: c.task_plan_synch(),
        release=None,
        checked=None,
        enabled=_always_enabled,
        label=None,
    ),
    "override_limits": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=lambda b, c: c.override_limits(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.OVERRIDE_LIMITS),
        label=None,
    ),
    "reset_interpreter": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=lambda b, c: c.reset_interpreter(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.RESET_INTERPRETER),
        label=None,
    ),
    "auto_reverse": _ActionSpec(
        style="trigger",
        signals=_program_signals(),
        dispatch=lambda b, c: c.auto_reverse(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.AUTO_REVERSE),
        label=None,
    ),
    "auto_forward": _ActionSpec(
        style="trigger",
        signals=_program_signals(),
        dispatch=lambda b, c: c.auto_forward(),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.AUTO_FORWARD),
        label=None,
    ),
    "set_min_limit": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_set_min_limit_dispatch,
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.SET_MIN_LIMIT),
        label=None,
        relevant_props=("axis", "value"),
    ),
    "set_max_limit": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_set_max_limit_dispatch,
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.SET_MAX_LIMIT),
        label=None,
        relevant_props=("axis", "value"),
    ),
    "set_digital_output": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_set_digital_output_dispatch,
        release=None,
        checked=None,
        enabled=_always_enabled,
        label=None,
        relevant_props=("index", "target"),
    ),
    "set_analog_output": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_set_analog_output_dispatch,
        release=None,
        checked=None,
        enabled=_always_enabled,
        label=None,
        relevant_props=("index", "value"),
    ),
    "error_msg": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_send_error_msg_dispatch,
        release=None,
        checked=None,
        enabled=_always_enabled,
        label=None,
        relevant_props=("message",),
    ),
    "text_msg": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_send_text_msg_dispatch,
        release=None,
        checked=None,
        enabled=_always_enabled,
        label=None,
        relevant_props=("message",),
    ),
    "display_msg": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_send_display_msg_dispatch,
        release=None,
        checked=None,
        enabled=_always_enabled,
        label=None,
        relevant_props=("message",),
    ),
    "spindle_increase": _ActionSpec(
        style="trigger",
        signals=("spindles_changed", "machine_state_changed"),
        dispatch=lambda b, c: c.spindle_increase(index=b.index),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.SPINDLE_INCREASE),
        label=None,
        relevant_props=("index",),
    ),
    "spindle_decrease": _ActionSpec(
        style="trigger",
        signals=("spindles_changed", "machine_state_changed"),
        dispatch=lambda b, c: c.spindle_decrease(index=b.index),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.SPINDLE_DECREASE),
        label=None,
        relevant_props=("index",),
    ),
    "spindle_constant": _ActionSpec(
        style="trigger",
        signals=("spindles_changed", "machine_state_changed"),
        dispatch=lambda b, c: c.spindle_constant(index=b.index),
        release=None,
        checked=None,
        enabled=_verb_enabled(CommandVerb.SPINDLE_CONSTANT),
        label=None,
        relevant_props=("index",),
    ),
    "debug": _ActionSpec(
        style="trigger",
        signals=("machine_state_changed",),
        dispatch=_set_debug_dispatch,
        release=None,
        checked=None,
        enabled=_always_enabled,
        label=None,
        relevant_props=("mask",),
    ),
}


_VALID_ACTIONS: tuple[str, ...] = tuple(_ACTION_SPECS.keys())

_AUX_PROPS: frozenset[str] = frozenset({
    "target", "axis", "joint", "direction", "velocity", "distance", "speed",
    "index", "mdi_line", "path", "value", "message", "mask", "confirm",
})

ActionVerb = _register_enum(
    IntEnum("ActionVerb", [(name, i) for i, name in enumerate(_VALID_ACTIONS)]),
)


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class ActionButton(QtcncWidget, QPushButton):
    """One push button bound to one qtcnc Command action."""

    ActionVerb = ActionVerb
    if _Q_ENUMS is not None:
        _Q_ENUMS(ActionVerb)

    physical_pressed = Signal()
    physical_released = Signal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._action: ActionVerb = ActionVerb.estop
        self._target: str = ""
        self._axis: int = 0
        self._joint: int = -1
        self._direction: int = 1
        self._velocity: float = 60.0
        self._distance: float = 1.0
        self._speed: float = 1000.0
        self._index: int = 0
        self._mdi_line: str = ""
        self._path: str = ""
        self._last_dir: str = ""
        self._value: float = 0.0
        self._message: str = ""
        self._mask: int = 0
        self._confirm: bool = False
        self._apply_style()
        self.clicked.connect(self._on_clicked)
        self.physical_pressed.connect(self._on_physical_pressed)
        self.physical_released.connect(self._on_physical_released)

    # ----- spec access -----

    def _spec(self) -> _ActionSpec:
        return _ACTION_SPECS[self._action.name]

    @classmethod
    def relevant_property_names(cls, action: "ActionVerb | str | int") -> frozenset[str]:
        """Auxiliary property names that the given action reads.

        Used by the Designer property-sheet extension to hide irrelevant
        properties from the inspector when the user picks an action.
        The `action` property itself is always relevant.
        """
        if isinstance(action, ActionVerb):
            name = action.name
        elif isinstance(action, str):
            name = action
        else:
            name = ActionVerb(int(action)).name
        spec = _ACTION_SPECS[name]
        return frozenset({"action", *spec.relevant_props})

    @classmethod
    def all_auxiliary_property_names(cls) -> frozenset[str]:
        """Every auxiliary property the Designer exposes on ActionButton."""
        return _AUX_PROPS

    def _apply_style(self) -> None:
        spec = self._spec()
        self.setCheckable(
            spec.style in ("toggle", "radio") and spec.checked is not None
        )
        self.setAutoExclusive(False)
        self.setAutoRepeat(False)

    def nextCheckState(self) -> None:
        spec = self._spec()
        if spec.checked is None:
            return
        if spec.style == "toggle":
            self.setChecked(not self.isChecked())
        elif spec.style == "radio" and not self.isChecked():
            self.setChecked(True)

    # ----- Qt properties -----

    def _get_action(self) -> "ActionVerb":
        return self._action

    def _set_action(self, value: Any) -> None:
        if isinstance(value, ActionVerb):
            verb = value
        elif isinstance(value, str):
            try:
                verb = ActionVerb[value]
            except KeyError:
                raise ValueError(
                    f"action must be one of {_VALID_ACTIONS}, got {value!r}"
                )
        else:
            try:
                verb = ActionVerb(int(value))
            except (ValueError, TypeError):
                raise ValueError(
                    f"action must be one of {_VALID_ACTIONS}, got {value!r}"
                )
        self._action = verb
        self._apply_style()

    action = Property(ActionVerb, _get_action, _set_action, designable=False)

    def _get_target(self) -> str:
        return self._target

    def _set_target(self, value: str) -> None:
        self._target = str(value)

    target = Property(str, _get_target, _set_target)

    def _get_axis(self) -> int:
        return self._axis

    def _set_axis(self, value: int) -> None:
        self._axis = int(value)

    axis = Property(int, _get_axis, _set_axis)

    def _get_joint(self) -> int:
        return self._joint

    def _set_joint(self, value: int) -> None:
        self._joint = int(value)

    joint = Property(int, _get_joint, _set_joint)

    def _get_direction(self) -> int:
        return self._direction

    def _set_direction(self, value: int) -> None:
        self._direction = +1 if int(value) >= 0 else -1

    direction = Property(int, _get_direction, _set_direction)

    def _get_velocity(self) -> float:
        return self._velocity

    def _set_velocity(self, value: float) -> None:
        self._velocity = float(value)

    velocity = Property(float, _get_velocity, _set_velocity)

    def _get_distance(self) -> float:
        return self._distance

    def _set_distance(self, value: float) -> None:
        self._distance = float(value)

    distance = Property(float, _get_distance, _set_distance)

    def _get_speed(self) -> float:
        return self._speed

    def _set_speed(self, value: float) -> None:
        self._speed = float(value)

    speed = Property(float, _get_speed, _set_speed)

    def _get_index(self) -> int:
        return self._index

    def _set_index(self, value: int) -> None:
        self._index = int(value)

    index = Property(int, _get_index, _set_index)

    def _get_mdi_line(self) -> str:
        return self._mdi_line

    def _set_mdi_line(self, value: str) -> None:
        self._mdi_line = str(value)

    mdi_line = Property(str, _get_mdi_line, _set_mdi_line)

    def _get_path(self) -> str:
        return self._path

    def _set_path(self, value: str) -> None:
        self._path = str(value)

    path = Property(str, _get_path, _set_path)

    def _get_value(self) -> float:
        return self._value

    def _set_value(self, value: float) -> None:
        self._value = float(value)

    value = Property(float, _get_value, _set_value)

    def _get_message(self) -> str:
        return self._message

    def _set_message(self, value: str) -> None:
        self._message = str(value)

    message = Property(str, _get_message, _set_message)

    def _get_mask(self) -> int:
        return self._mask

    def _set_mask(self, value: int) -> None:
        self._mask = int(value)

    mask = Property(int, _get_mask, _set_mask)

    def _get_confirm(self) -> bool:
        return self._confirm

    def _set_confirm(self, value: bool) -> None:
        self._confirm = bool(value)

    confirm = Property(bool, _get_confirm, _set_confirm)

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
        if spec.checked is not None:
            want = spec.checked(self, state)
            if self.isChecked() != want:
                self.setChecked(want)
        self.setEnabled(spec.enabled(self, state))
        if spec.label is not None:
            new_label = spec.label(self, state)
            if new_label is not None:
                self.setText(new_label)

    # ----- click dispatch -----

    def _on_clicked(self) -> None:
        spec = self._spec()
        if spec.style == "momentary":
            return
        spec.dispatch(self, self.window().qtcnc_command)

    # ----- momentary press/release -----

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.physical_pressed.emit()

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.physical_released.emit()

    def _on_physical_pressed(self) -> None:
        spec = self._spec()
        if spec.style != "momentary":
            return
        spec.dispatch(self, self.window().qtcnc_command)

    def _on_physical_released(self) -> None:
        spec = self._spec()
        if spec.style != "momentary" or spec.release is None:
            return
        spec.release(self, self.window().qtcnc_command)
