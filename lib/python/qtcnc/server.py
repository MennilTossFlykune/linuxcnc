"""qtcnc-serverd: the daemon that owns `linuxcnc.*` and `hal.component`.

This module is the *only* place in the qtcnc codebase that imports
`linuxcnc` or `hal`. The GUI client speaks to it over ZMQ and never
touches LinuxCNC directly. The daemon:

1. Owns a `linuxcnc.stat()` / `linuxcnc.command()` / `linuxcnc.error_channel()`
   trio.
2. Owns a single `hal.component("qtcnc")` (or a caller-supplied name).
3. Polls `stat()` on a background thread at ~20 Hz, diffs against the
   previous snapshot, and publishes `state_diff` PUB messages.
4. Drains `error_channel().poll()` in the same loop and publishes
   typed `ErrorMessage`s.
5. Accepts `DECLARE_PINS` once, calls `halcomp.ready()`, and then runs
   any `[HAL]POSTGUI_HALFILE` entries from the INI — exactly like
   `qtvcp.postgui` in `src/emc/usr_intf/qtvcp/qtvcp.py`.
6. Maps `EXEC_COMMAND` verbs to `linuxcnc.command()` calls.
7. Handles `SUBSCRIBE_PIN` for foreign pins (owned by other HAL
   components) by polling them and publishing updates on change.

The `linuxcnc` and `hal` modules are *injected* via the constructor so
tests can pass in fakes. `main()` imports the real modules; nothing
else in this file does.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Optional

from qtcnc import PROTOCOL_VERSION
from qtcnc.core.command import _VERB_TO_PREDICATE
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.state import StateStore, diff
from qtcnc.core.types import (
    AxisState,
    CoolantState,
    ErrorMessage,
    ErrorSeverity,
    ExecState,
    InterpSettings,
    InterpState,
    IoState,
    JointState,
    LinearUnits,
    MachineState,
    MotionMode,
    MotionType,
    Offsets,
    Overrides,
    Position,
    ProbeState,
    ProgramState,
    ProgramUnits,
    SpindleDir,
    SpindleState,
    TaskInfo,
    TaskMode,
    TaskState,
    Tool,
    ToolEntry,
)
from qtcnc.signals import CommandVerb, Lifecycle
from qtcnc.transport.base import DeclarePinsResult, NackError
from qtcnc.transport.zmq_server import ZmqServerTransport

_log = logging.getLogger("qtcnc.server")


# ---------------------------------------------------------------------------
# stat -> StateStore mapping
# ---------------------------------------------------------------------------


def _position_from_tuple(raw: Any) -> Position:
    """Map a 9-float tuple (as produced by stat.actual_position) to Position."""
    t = tuple(raw) if raw is not None else ()

    def at(i: int, default: Any = None) -> Any:
        if i < len(t):
            return float(t[i])
        return default

    return Position(
        x=at(0, 0.0) or 0.0,
        y=at(1, 0.0) or 0.0,
        z=at(2, 0.0) or 0.0,
        a=at(3),
        b=at(4),
        c=at(5),
        u=at(6),
        v=at(7),
        w=at(8),
    )


def _task_state_map(lc: Any) -> dict[int, TaskState]:
    return {
        lc.STATE_ESTOP: TaskState.ESTOP,
        lc.STATE_ESTOP_RESET: TaskState.ESTOP_RESET,
        lc.STATE_OFF: TaskState.OFF,
        lc.STATE_ON: TaskState.ON,
    }


def _task_mode_map(lc: Any) -> dict[int, TaskMode]:
    return {
        lc.MODE_MANUAL: TaskMode.MANUAL,
        lc.MODE_AUTO: TaskMode.AUTO,
        lc.MODE_MDI: TaskMode.MDI,
    }


def _interp_state_map(lc: Any) -> dict[int, InterpState]:
    return {
        lc.INTERP_IDLE: InterpState.IDLE,
        lc.INTERP_READING: InterpState.READING,
        lc.INTERP_PAUSED: InterpState.PAUSED,
        lc.INTERP_WAITING: InterpState.WAITING,
    }


def _get_from(obj: Any, key: str, default: Any) -> Any:
    """Dict-or-struct accessor for per-joint, per-axis, per-spindle entries."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _read_machine(
    stat: Any, lc: Any, coordinates: tuple[str, ...] = (),
) -> MachineState:
    ts_map = _task_state_map(lc)
    tm_map = _task_mode_map(lc)
    is_map = _interp_state_map(lc)

    task_state = ts_map.get(getattr(stat, "task_state", None), TaskState.ESTOP)
    task_mode = tm_map.get(getattr(stat, "task_mode", None), TaskMode.MANUAL)
    interp_state = is_map.get(getattr(stat, "interp_state", None), InterpState.IDLE)

    try:
        motion_type = MotionType(int(getattr(stat, "motion_type", 0) or 0))
    except ValueError:
        motion_type = MotionType.NONE

    estop = task_state == TaskState.ESTOP
    powered = task_state == TaskState.ON

    joint_count = int(getattr(stat, "joints", 0) or 0) or 3
    homed_raw = getattr(stat, "homed", ()) or ()
    homed = tuple(bool(h) for h in homed_raw[:joint_count])
    axis_count = joint_count

    try:
        motion_mode = MotionMode(int(getattr(stat, "motion_mode", 0) or MotionMode.FREE))
    except ValueError:
        motion_mode = MotionMode.FREE
    kinematics_type = int(getattr(stat, "kinematics_type", 1) or 1)
    kinematics_identity = kinematics_type == getattr(lc, "KINEMATICS_IDENTITY", 1)

    # `stat.linear_units` is "units per mm": 1.0 == mm, 1/25.4 ≈ 0.03937 == inch.
    # A 0.0 reading means the INI hasn't been loaded yet; treat as MM.
    raw_units = float(getattr(stat, "linear_units", 0.0) or 0.0)
    linear_units = LinearUnits.INCH if 0.01 < raw_units < 0.9 else LinearUnits.MM

    axis_mask = int(getattr(stat, "axis_mask", 0) or 0)
    if axis_mask == 0:
        axis_mask = 0b111  # sane default when the daemon hasn't seen a stat poll yet

    return MachineState(
        estop=estop,
        powered=powered,
        task_mode=task_mode,
        interp_state=interp_state,
        motion_type=motion_type,
        homed=homed,
        axis_count=axis_count,
        motion_mode=motion_mode,
        kinematics_identity=kinematics_identity,
        linear_units=linear_units,
        axis_mask=axis_mask,
        joint_count=joint_count,
        spindle_count=int(getattr(stat, "spindles", 0) or 0),
        num_extrajoints=int(getattr(stat, "num_extrajoints", 0) or 0),
        cycle_time=float(getattr(stat, "cycle_time", 0.0) or 0.0),
        linear_units_per_mm=float(raw_units or 1.0),
        angular_units_per_deg=float(getattr(stat, "angular_units", 1.0) or 1.0),
        kinematics_type=kinematics_type,
        motion_enabled=bool(getattr(stat, "enabled", False)),
        inpos=bool(getattr(stat, "inpos", False)),
        queue=int(getattr(stat, "queue", 0) or 0),
        active_queue=int(getattr(stat, "active_queue", 0) or 0),
        queue_full=bool(getattr(stat, "queue_full", False)),
        motion_id=int(getattr(stat, "motion_id", 0) or 0),
        single_stepping=bool(getattr(stat, "single_stepping", False)),
        commanded_velocity=float(getattr(stat, "velocity", 0.0) or 0.0),
        commanded_acceleration=float(getattr(stat, "acceleration", 0.0) or 0.0),
        max_acceleration=float(getattr(stat, "max_acceleration", 0.0) or 0.0),
        distance_to_go_scalar=float(getattr(stat, "distance_to_go", 0.0) or 0.0),
        coordinates=coordinates,
    )


def _read_program(stat: Any, interp_state: InterpState) -> ProgramState:
    # `read_line` is the line the interpreter is currently reading (queue-ahead),
    # which is what operators want to see highlighted. Fall back to
    # `current_line` if absent — older LinuxCNC versions may not export it.
    read_line = getattr(stat, "read_line", None)
    if read_line is None:
        current_line = int(getattr(stat, "current_line", 0) or 0)
    else:
        current_line = int(read_line or 0)
    # `is_running` covers both actively reading and waiting-for-motion so
    # the flag doesn't flicker while motion catches up. `is_paused` ORs the
    # task-level paused flag with the interp-level PAUSED state so either
    # source triggers the pause signal.
    is_running = interp_state in (InterpState.READING, InterpState.WAITING)
    is_paused = (
        bool(getattr(stat, "paused", False))
        or interp_state == InterpState.PAUSED
    )
    try:
        program_units = ProgramUnits(int(getattr(stat, "program_units", 2) or 2))
    except ValueError:
        program_units = ProgramUnits.MM
    return ProgramState(
        path=str(getattr(stat, "file", "") or ""),
        total_lines=0,
        current_line=current_line,
        is_running=is_running,
        is_paused=is_paused,
        motion_line=int(getattr(stat, "motion_line", 0) or 0),
        program_units=program_units,
    )


def _read_task_info(stat: Any) -> TaskInfo:
    try:
        exec_state = ExecState(int(getattr(stat, "exec_state", 2) or 2))
    except ValueError:
        exec_state = ExecState.DONE
    return TaskInfo(
        rcs_state=int(getattr(stat, "state", 0) or 0),
        echo_serial_number=int(getattr(stat, "echo_serial_number", 0) or 0),
        exec_state=exec_state,
        call_level=int(getattr(stat, "call_level", 0) or 0),
        current_line=int(getattr(stat, "current_line", 0) or 0),
        active_command_line=str(getattr(stat, "command", "") or ""),
        interpreter_errcode=int(getattr(stat, "interpreter_errcode", 0) or 0),
        optional_stop=bool(getattr(stat, "optional_stop", False)),
        block_delete=bool(getattr(stat, "block_delete", False)),
        task_paused=int(getattr(stat, "task_paused", 0) or 0),
        input_timeout=bool(getattr(stat, "input_timeout", False)),
        ini_filename=str(getattr(stat, "ini_filename", "") or ""),
        delay_left=float(getattr(stat, "delay_left", 0.0) or 0.0),
        queued_mdi_commands=int(getattr(stat, "queued_mdi_commands", 0) or 0),
        debug_mask=int(getattr(stat, "debug", 0) or 0),
    )


def _read_offsets(stat: Any) -> Offsets:
    return Offsets(
        g5x_index=int(getattr(stat, "g5x_index", 1) or 1),
        g5x=_position_from_tuple(getattr(stat, "g5x_offset", ())),
        g92=_position_from_tuple(getattr(stat, "g92_offset", ())),
        tool_offset=_position_from_tuple(getattr(stat, "tool_offset", ())),
        rotation_xy=float(getattr(stat, "rotation_xy", 0.0) or 0.0),
    )


def _read_active_settings(stat: Any) -> InterpSettings:
    raw = tuple(getattr(stat, "settings", ()) or ())

    def at(i: int, default: float = 0.0) -> float:
        return float(raw[i]) if i < len(raw) else default

    return InterpSettings(
        sequence_number=at(0),
        feed=at(1),
        speed=at(2),
        g64_blend_tolerance=at(3),
        naive_cam_tolerance=at(4),
    )


def _read_joints(stat: Any, count: int) -> tuple[JointState, ...]:
    raw = tuple(getattr(stat, "joint", ()) or ())
    out: list[JointState] = []
    for i in range(count):
        if i >= len(raw):
            out.append(JointState())
            continue
        d = raw[i]
        out.append(JointState(
            joint_type=int(_get_from(d, "jointType", 0) or 0),
            units=float(_get_from(d, "units", 1.0) or 1.0),
            backlash=float(_get_from(d, "backlash", 0.0) or 0.0),
            min_position_limit=float(_get_from(d, "min_position_limit", 0.0) or 0.0),
            max_position_limit=float(_get_from(d, "max_position_limit", 0.0) or 0.0),
            max_ferror=float(_get_from(d, "max_ferror", 0.0) or 0.0),
            min_ferror=float(_get_from(d, "min_ferror", 0.0) or 0.0),
            ferror_current=float(_get_from(d, "ferror_current", 0.0) or 0.0),
            ferror_highmark=float(_get_from(d, "ferror_highmark", 0.0) or 0.0),
            output=float(_get_from(d, "output", 0.0) or 0.0),
            input=float(_get_from(d, "input", 0.0) or 0.0),
            velocity=float(_get_from(d, "velocity", 0.0) or 0.0),
            inpos=bool(_get_from(d, "inpos", False)),
            homing_state=int(_get_from(d, "homing", 0) or 0),
            homed=bool(_get_from(d, "homed", False)),
            fault=bool(_get_from(d, "fault", False)),
            enabled=bool(_get_from(d, "enabled", False)),
            min_soft_limit=bool(_get_from(d, "min_soft_limit", False)),
            max_soft_limit=bool(_get_from(d, "max_soft_limit", False)),
            min_hard_limit=bool(_get_from(d, "min_hard_limit", False)),
            max_hard_limit=bool(_get_from(d, "max_hard_limit", False)),
            override_limits=bool(_get_from(d, "override_limits", False)),
        ))
    return tuple(out)


def _read_axes(stat: Any, mask: int) -> tuple[AxisState, ...]:
    raw = tuple(getattr(stat, "axis", ()) or ())
    out: list[AxisState] = []
    for i in range(9):
        if not (mask & (1 << i)):
            continue
        if i >= len(raw):
            out.append(AxisState())
            continue
        d = raw[i]
        out.append(AxisState(
            velocity=float(_get_from(d, "velocity", 0.0) or 0.0),
            min_position_limit=float(_get_from(d, "min_position_limit", 0.0) or 0.0),
            max_position_limit=float(_get_from(d, "max_position_limit", 0.0) or 0.0),
        ))
    return tuple(out)


def _read_tool_table(stat: Any) -> tuple[ToolEntry, ...]:
    raw = tuple(getattr(stat, "tool_table", ()) or ())
    out: list[ToolEntry] = []
    for row in raw:
        # Rows are PyStructSequence (numeric indexing) from the C module,
        # or tuples in tests. Skip empty/placeholder rows with id <= 0.
        try:
            tool_id = int(row[0])
        except (TypeError, IndexError):
            continue
        if tool_id <= 0:
            continue
        out.append(ToolEntry(
            id=tool_id,
            offset=Position(
                x=float(row[1]),
                y=float(row[2]),
                z=float(row[3]),
                a=float(row[4]),
                b=float(row[5]),
                c=float(row[6]),
                u=float(row[7]),
                v=float(row[8]),
                w=float(row[9]),
            ),
            diameter=float(row[10]),
            frontangle=float(row[11]),
            backangle=float(row[12]),
            orientation=int(row[13]),
        ))
    return tuple(out)


def _read_coolant(stat: Any) -> CoolantState:
    return CoolantState(
        mist=bool(getattr(stat, "mist", 0)),
        flood=bool(getattr(stat, "flood", 0)),
    )


def _read_probe(stat: Any) -> ProbeState:
    return ProbeState(
        tripped=bool(getattr(stat, "probe_tripped", False)),
        probing=bool(getattr(stat, "probing", False)),
        value=int(getattr(stat, "probe_val", 0) or 0),
        probed_position=_position_from_tuple(getattr(stat, "probed_position", ())),
    )


def _read_io(stat: Any) -> IoState:
    return IoState(
        digital_in=tuple(bool(x) for x in (getattr(stat, "din", ()) or ())),
        digital_out=tuple(bool(x) for x in (getattr(stat, "dout", ()) or ())),
        analog_in=tuple(float(x) for x in (getattr(stat, "ain", ()) or ())),
        analog_out=tuple(float(x) for x in (getattr(stat, "aout", ()) or ())),
        misc_error=tuple(int(x) for x in (getattr(stat, "misc_error", ()) or ())),
        pocket_prepped=int(getattr(stat, "pocket_prepped", -1)),
        tool_from_pocket=int(getattr(stat, "tool_from_pocket", 0) or 0),
        aux_estop=bool(getattr(stat, "estop", False)),
    )


def _read_spindles(
    stat: Any,
) -> tuple[tuple[SpindleState, ...], tuple[float, ...]]:
    raw_spindles = getattr(stat, "spindle", ()) or ()
    spindles: list[SpindleState] = []
    spindle_overrides: list[float] = []
    for i, sp in enumerate(raw_spindles):
        try:
            direction = SpindleDir(int(_get_from(sp, "direction", 0)))
        except ValueError:
            direction = SpindleDir.STOP
        spindles.append(SpindleState(
            index=i,
            speed=float(_get_from(sp, "speed", 0.0) or 0.0),
            direction=direction,
            enabled=bool(_get_from(sp, "enabled", False)),
            brake=bool(_get_from(sp, "brake", False)),
            override_enabled=bool(_get_from(sp, "override_enabled", True)),
            homed=bool(_get_from(sp, "homed", False)),
            orient_state=int(_get_from(sp, "orient_state", 0) or 0),
            orient_fault=int(_get_from(sp, "orient_fault", 0) or 0),
        ))
        spindle_overrides.append(float(_get_from(sp, "override", 1.0) or 1.0))
    if not spindles:
        spindles = [SpindleState()]
        spindle_overrides = [1.0]
    return tuple(spindles), tuple(spindle_overrides)


def _resolve_in_spindle_tool(
    tool_id: int, tool_table: tuple[ToolEntry, ...],
) -> Tool:
    if tool_id <= 0 or not tool_table:
        return Tool(id=tool_id)
    for entry in tool_table:
        if entry.id == tool_id:
            return Tool(
                id=entry.id,
                pocket=0,
                offset=entry.offset,
                diameter=entry.diameter,
                frontangle=entry.frontangle,
                backangle=entry.backangle,
                orientation=entry.orientation,
                comment="",
            )
    return Tool(id=tool_id)


def stat_to_state_store(
    stat: Any, lc: Any, coordinates: tuple[str, ...] = (),
) -> StateStore:
    """Build a StateStore snapshot from a polled `linuxcnc.stat` object.

    This function is the single source of truth for the mapping. It is
    exported so tests can exercise it directly with a fake stat. Work
    is delegated to per-group helpers that each return a single
    sub-dataclass or tuple.
    """
    machine = _read_machine(stat, lc, coordinates)
    program = _read_program(stat, machine.interp_state)
    task_info = _read_task_info(stat)
    offsets = _read_offsets(stat)
    active_settings = _read_active_settings(stat)
    joints = _read_joints(stat, machine.joint_count)
    axes = _read_axes(stat, machine.axis_mask)
    tool_table = _read_tool_table(stat)
    coolant = _read_coolant(stat)
    probe = _read_probe(stat)
    io = _read_io(stat)
    spindles, spindle_overrides = _read_spindles(stat)

    position = _position_from_tuple(getattr(stat, "actual_position", ()))
    machine_position = _position_from_tuple(
        getattr(stat, "joint_actual_position", ()) or getattr(stat, "position", ())
    )
    dtg = _position_from_tuple(getattr(stat, "dtg", ()))
    commanded_position = _position_from_tuple(getattr(stat, "position", ()))

    overrides = Overrides(
        feed=float(getattr(stat, "feedrate", 1.0) or 1.0),
        rapid=float(getattr(stat, "rapidrate", 1.0) or 1.0),
        spindles=spindle_overrides,
        max_velocity=float(getattr(stat, "max_velocity", 0.0) or 0.0),
        feed_enabled=bool(getattr(stat, "feed_override_enabled", True)),
        adaptive_enabled=bool(getattr(stat, "adaptive_feed_enabled", False)),
        hold_enabled=bool(getattr(stat, "feed_hold_enabled", True)),
    )

    tool_in_spindle = int(getattr(stat, "tool_in_spindle", 0) or 0)
    tool = _resolve_in_spindle_tool(tool_in_spindle, tool_table)

    active_gcodes = tuple(
        int(g) for g in (getattr(stat, "gcodes", ()) or ()) if g is not None and g != -1
    )
    active_mcodes = tuple(
        int(m) for m in (getattr(stat, "mcodes", ()) or ()) if m is not None and m != -1
    )

    return StateStore(
        connected=True,
        task_state=_task_state_map(lc).get(
            getattr(stat, "task_state", None), TaskState.ESTOP,
        ),
        machine=machine,
        position=position,
        machine_position=machine_position,
        dtg=dtg,
        program=program,
        tool=tool,
        tool_in_spindle=tool_in_spindle,
        spindles=spindles,
        overrides=overrides,
        feed_rate=float(getattr(stat, "current_vel", 0.0) or 0.0),
        rapid_rate=float(getattr(stat, "max_velocity", 0.0) or 0.0),
        active_gcodes=active_gcodes,
        active_mcodes=active_mcodes,
        task_info=task_info,
        offsets=offsets,
        active_settings=active_settings,
        joints=joints,
        axes=axes,
        tool_table=tool_table,
        coolant=coolant,
        probe=probe,
        io=io,
        commanded_position=commanded_position,
        heartbeat=int(getattr(stat, "heartbeat", 0) or 0),
        taskbeat=int(getattr(stat, "taskbeat", 0) or 0),
    )


# ---------------------------------------------------------------------------
# CommandVerb -> linuxcnc.command() dispatch
# ---------------------------------------------------------------------------


def _resolve_jog_mode(
    lc: Any, cmd: Any, machine: MachineState,
) -> tuple[int, MotionMode]:
    """Return `(jjogmode, target_motion_mode)` for the next jog command.

    Only issues `teleop_enable` + `wait_complete` when the current
    `machine.motion_mode` differs from the target. Callers record the
    target on the returned `StateStore` so subsequent jogs skip the
    blocking mode switch.
    """
    if machine.can_use_teleop_jog:
        jjogmode = 0
        target = MotionMode.TELEOP
        target_int = lc.TRAJ_MODE_TELEOP
    else:
        jjogmode = 1
        target = MotionMode.FREE
        target_int = lc.TRAJ_MODE_FREE
    if machine.motion_mode != target:
        cmd.teleop_enable(1 if target == MotionMode.TELEOP else 0)
        cmd.wait_complete()
    return jjogmode, target


def _jog_target(
    jjogmode: int, kwargs: dict[str, Any], machine: MachineState,
) -> int:
    """Pick the axis index or joint number for a jog command.

    In teleop mode (jjogmode=0), returns the ``axis`` kwarg (axis index).
    In free mode (jjogmode=1), returns the ``joint`` kwarg if the client
    sent one (≥ 0), otherwise converts ``axis`` → joint via the
    coordinates mapping.
    """
    axis = int(kwargs.get("axis", 0))
    if jjogmode == 0:
        return axis
    joint = int(kwargs.get("joint", -1))
    if joint >= 0:
        return joint
    return machine.joint_for_axis(axis)


def _require(cond: bool, reason: str) -> None:
    """Raise NackError(reason) if `cond` is false."""
    if not cond:
        raise NackError(reason)


def _check_verb(state: StateStore, verb: CommandVerb) -> None:
    """Look up `verb` in the canonical `_VERB_TO_PREDICATE` table and
    raise NackError with the table's reason string if the predicate
    refuses. Verbs absent from the table are unconditionally permitted.
    """
    entry = _VERB_TO_PREDICATE.get(verb)
    if entry is None:
        return
    predicate, reason = entry
    if not predicate(state):
        raise NackError(reason)


def _require_mode(
    state: StateStore,
    cmd: Any,
    lc: Any,
    target: TaskMode,
    reason: str,
    *,
    auto_switch: bool,
) -> None:
    """Ensure `state.machine.task_mode == target`.

    Without `auto_switch`, a mismatch raises `NackError(reason)`. With
    `auto_switch=True`, a mismatch raises `NotImplementedError`; the
    mode-switch body is reserved for a future commit. Callers that pass
    `auto_switch=True` today learn loudly that the path is not wired.
    """
    if state.machine.task_mode == target:
        return
    if not auto_switch:
        raise NackError(reason)
    raise NotImplementedError(
        "auto_switch_mode requested but not enabled in this build",
    )


def execute_command(
    state: StateStore,
    cmd: Any,
    lc: Any,
    verb: CommandVerb,
    kwargs: dict[str, Any],
    *,
    auto_switch_mode: bool = False,
) -> StateStore:
    """Translate a `CommandVerb` into the equivalent `linuxcnc.command()` call.

    Checks the state-predicate guard for each verb before dispatch.
    Returns a `StateStore` reflecting any motion_mode change `_resolve_jog_mode`
    performed. Non-jog verbs return `state` untouched so callers can
    unconditionally write the result back.

    Raises `NackError(reason)` when the guard refuses. Reason strings
    follow the `<verb>_requires_<precondition>` form so widgets can match
    them without parsing natural-language text. The guard table lives
    in `core.command._VERB_TO_PREDICATE` and is shared with the client
    so daemon and client never drift.
    """
    _check_verb(state, verb)
    machine = state.machine

    # cmd.state(lc.STATE_*) — emcmodule.cc:3148-3151
    if verb == CommandVerb.STATE_ESTOP:
        cmd.state(lc.STATE_ESTOP)
    elif verb == CommandVerb.STATE_ESTOP_RESET:
        cmd.state(lc.STATE_ESTOP_RESET)
    elif verb == CommandVerb.STATE_ON:
        cmd.state(lc.STATE_ON)
    elif verb == CommandVerb.STATE_OFF:
        cmd.state(lc.STATE_OFF)
    # cmd.mode(lc.MODE_*) — emcmodule.cc:3144-3146
    elif verb == CommandVerb.SET_MODE:
        mode = kwargs.get("mode")
        mode_constants = {
            TaskMode.MANUAL: lc.MODE_MANUAL,
            TaskMode.AUTO: lc.MODE_AUTO,
            TaskMode.MDI: lc.MODE_MDI,
        }
        if not isinstance(mode, TaskMode):
            raise NackError(f"set_mode requires TaskMode, got {mode!r}")
        cmd.mode(mode_constants[mode])
    # cmd.home(joint) / cmd.unhome(joint) — joint=-1 means all joints.
    # Both require FREE (joint) traj mode; after homing LinuxCNC
    # auto-switches to TELEOP so we must switch back.
    elif verb == CommandVerb.HOME:
        if machine.motion_mode != MotionMode.FREE:
            cmd.teleop_enable(0)
            cmd.wait_complete()
        cmd.home(int(kwargs.get("joint", -1)))
        return replace(state, machine=replace(machine, motion_mode=MotionMode.FREE))
    elif verb == CommandVerb.UNHOME:
        if machine.motion_mode != MotionMode.FREE:
            cmd.teleop_enable(0)
            cmd.wait_complete()
        cmd.unhome(int(kwargs.get("joint", -1)))
        return replace(state, machine=replace(machine, motion_mode=MotionMode.FREE))
    # cmd.jog(lc.JOG_*, jjogmode, axis_or_joint, …) — emcmodule.cc:3169-3171
    # In teleop (jjogmode=0) the third param is an axis index; in free
    # (jjogmode=1) it is a joint number.
    elif verb == CommandVerb.JOG_CONTINUOUS:
        jjogmode, new_mode = _resolve_jog_mode(lc, cmd, machine)
        target = _jog_target(jjogmode, kwargs, machine)
        cmd.jog(
            lc.JOG_CONTINUOUS, jjogmode, target,
            float(kwargs.get("velocity", 0.0)),
        )
        return replace(state, machine=replace(machine, motion_mode=new_mode))
    elif verb == CommandVerb.JOG_STOP:
        jjogmode, new_mode = _resolve_jog_mode(lc, cmd, machine)
        target = _jog_target(jjogmode, kwargs, machine)
        cmd.jog(lc.JOG_STOP, jjogmode, target)
        return replace(state, machine=replace(machine, motion_mode=new_mode))
    elif verb == CommandVerb.JOG_INCREMENT:
        jjogmode, new_mode = _resolve_jog_mode(lc, cmd, machine)
        target = _jog_target(jjogmode, kwargs, machine)
        cmd.jog(
            lc.JOG_INCREMENT, jjogmode, target,
            float(kwargs.get("velocity", 0.0)),
            float(kwargs.get("distance", 0.0)),
        )
        return replace(state, machine=replace(machine, motion_mode=new_mode))
    # cmd.feedrate / rapidrate / spindleoverride — emcmodule.cc:1491-1505
    elif verb == CommandVerb.FEEDRATE:
        cmd.feedrate(float(kwargs.get("scale", 1.0)))
    elif verb == CommandVerb.RAPIDRATE:
        cmd.rapidrate(float(kwargs.get("scale", 1.0)))
    elif verb == CommandVerb.SPINDLEOVERRIDE:
        cmd.spindleoverride(
            float(kwargs.get("scale", 1.0)), int(kwargs.get("index", 0)),
        )
    # cmd.auto(lc.AUTO_*) — emcmodule.cc:3173-3178 / emcauto:1861
    elif verb == CommandVerb.AUTO_RUN:
        if state.missing_tools:
            raise NackError(
                f"auto_run_requires_all_tools: missing {sorted(state.missing_tools)}"
            )
        cmd.auto(lc.AUTO_RUN, int(kwargs.get("line", 0)))
    elif verb == CommandVerb.AUTO_PAUSE:
        cmd.auto(lc.AUTO_PAUSE)
    elif verb == CommandVerb.AUTO_RESUME:
        cmd.auto(lc.AUTO_RESUME)
    elif verb == CommandVerb.AUTO_STEP:
        cmd.auto(lc.AUTO_STEP)
    elif verb == CommandVerb.AUTO_REVERSE:
        cmd.auto(lc.AUTO_REVERSE)
    elif verb == CommandVerb.AUTO_FORWARD:
        cmd.auto(lc.AUTO_FORWARD)
    # cmd.abort() / cmd.mdi(text) — emcmodule.cc:Command_methods
    elif verb == CommandVerb.ABORT:
        cmd.abort()
    elif verb == CommandVerb.MDI:
        cmd.mdi(str(kwargs.get("command", "")))
    # cmd.spindle(lc.SPINDLE_*, …) — emcmodule.cc:3153-3158
    elif verb == CommandVerb.SPINDLE_FORWARD:
        cmd.spindle(
            lc.SPINDLE_FORWARD,
            float(kwargs.get("speed", 0.0)),
            int(kwargs.get("index", 0)),
        )
    elif verb == CommandVerb.SPINDLE_REVERSE:
        cmd.spindle(
            lc.SPINDLE_REVERSE,
            float(kwargs.get("speed", 0.0)),
            int(kwargs.get("index", 0)),
        )
    elif verb == CommandVerb.SPINDLE_OFF:
        cmd.spindle(lc.SPINDLE_OFF, 0.0, int(kwargs.get("index", 0)))
    elif verb == CommandVerb.SPINDLE_INCREASE:
        cmd.spindle(lc.SPINDLE_INCREASE, int(kwargs.get("index", 0)))
    elif verb == CommandVerb.SPINDLE_DECREASE:
        cmd.spindle(lc.SPINDLE_DECREASE, int(kwargs.get("index", 0)))
    elif verb == CommandVerb.SPINDLE_CONSTANT:
        cmd.spindle(lc.SPINDLE_CONSTANT, int(kwargs.get("index", 0)))
    # cmd.mist(lc.MIST_*) / cmd.flood(lc.FLOOD_*) — emcmodule.cc:3160-3164
    elif verb == CommandVerb.MIST_ON:
        cmd.mist(lc.MIST_ON)
    elif verb == CommandVerb.MIST_OFF:
        cmd.mist(lc.MIST_OFF)
    elif verb == CommandVerb.FLOOD_ON:
        cmd.flood(lc.FLOOD_ON)
    elif verb == CommandVerb.FLOOD_OFF:
        cmd.flood(lc.FLOOD_OFF)
    # cmd.brake(lc.BRAKE_*) — emcmodule.cc:3166-3167
    elif verb == CommandVerb.BRAKE_ENGAGE:
        cmd.brake(lc.BRAKE_ENGAGE)
    elif verb == CommandVerb.BRAKE_RELEASE:
        cmd.brake(lc.BRAKE_RELEASE)
    # Stand-alone cmd methods — emcmodule.cc:Command_methods (2077-2139)
    elif verb == CommandVerb.DEBUG:
        cmd.debug(int(kwargs.get("mask", 0)))
    elif verb == CommandVerb.TRAJ_MODE:
        mode = kwargs.get("mode")
        if not isinstance(mode, MotionMode):
            raise NackError(f"traj_mode requires MotionMode, got {mode!r}")
        mode_constants = {
            MotionMode.FREE: lc.TRAJ_MODE_FREE,
            MotionMode.COORD: lc.TRAJ_MODE_COORD,
            MotionMode.TELEOP: lc.TRAJ_MODE_TELEOP,
        }
        cmd.traj_mode(mode_constants[mode])
        return replace(state, machine=replace(machine, motion_mode=mode))
    elif verb == CommandVerb.MAXVEL:
        cmd.maxvel(float(kwargs.get("value", 0.0)))
    elif verb == CommandVerb.TOOL_OFFSET:
        cmd.tool_offset(
            int(kwargs.get("tool", 0)),
            float(kwargs.get("zoffset", 0.0)),
            float(kwargs.get("xoffset", 0.0)),
            float(kwargs.get("diameter", 0.0)),
            float(kwargs.get("frontangle", 0.0)),
            float(kwargs.get("backangle", 0.0)),
            int(kwargs.get("orientation", 0)),
        )
    elif verb == CommandVerb.LOAD_TOOL_TABLE:
        cmd.load_tool_table()
    elif verb == CommandVerb.TASK_PLAN_SYNCH:
        cmd.task_plan_synch()
    elif verb == CommandVerb.OVERRIDE_LIMITS:
        cmd.override_limits()
    elif verb == CommandVerb.RESET_INTERPRETER:
        cmd.reset_interpreter()
    elif verb == CommandVerb.SET_OPTIONAL_STOP:
        cmd.set_optional_stop(1 if bool(kwargs.get("enabled", False)) else 0)
    elif verb == CommandVerb.SET_BLOCK_DELETE:
        cmd.set_block_delete(1 if bool(kwargs.get("enabled", False)) else 0)
    elif verb == CommandVerb.SET_MIN_LIMIT:
        cmd.set_min_limit(
            int(kwargs.get("joint", 0)), float(kwargs.get("value", 0.0)),
        )
    elif verb == CommandVerb.SET_MAX_LIMIT:
        cmd.set_max_limit(
            int(kwargs.get("joint", 0)), float(kwargs.get("value", 0.0)),
        )
    elif verb == CommandVerb.SET_FEED_OVERRIDE:
        cmd.set_feed_override(1 if bool(kwargs.get("enabled", True)) else 0)
    elif verb == CommandVerb.SET_SPINDLE_OVERRIDE:
        cmd.set_spindle_override(
            1 if bool(kwargs.get("enabled", True)) else 0,
            int(kwargs.get("index", 0)),
        )
    elif verb == CommandVerb.SET_FEED_HOLD:
        cmd.set_feed_hold(1 if bool(kwargs.get("enabled", True)) else 0)
    elif verb == CommandVerb.SET_ADAPTIVE_FEED:
        cmd.set_adaptive_feed(1 if bool(kwargs.get("enabled", False)) else 0)
    elif verb == CommandVerb.SET_DIGITAL_OUTPUT:
        cmd.set_digital_output(
            int(kwargs.get("index", 0)),
            1 if bool(kwargs.get("value", False)) else 0,
        )
    elif verb == CommandVerb.SET_ANALOG_OUTPUT:
        cmd.set_analog_output(
            int(kwargs.get("index", 0)), float(kwargs.get("value", 0.0)),
        )
    elif verb == CommandVerb.ERROR_MSG:
        cmd.error_msg(str(kwargs.get("text", "")))
    elif verb == CommandVerb.TEXT_MSG:
        cmd.text_msg(str(kwargs.get("text", "")))
    elif verb == CommandVerb.DISPLAY_MSG:
        cmd.display_msg(str(kwargs.get("text", "")))
    elif verb == CommandVerb.SET_PROGRAM_TOOLS:
        raw = kwargs.get("tools", ())
        tools = frozenset(int(t) for t in raw if int(t) > 0)
        return replace(
            state,
            program=replace(state.program, requested_tools=tools),
        )
    else:
        raise NackError(f"unsupported verb: {verb}")
    return state


# ---------------------------------------------------------------------------
# HAL mapping
# ---------------------------------------------------------------------------


def _hal_type_constant(hal_mod: Any, t: HalType) -> int:
    return {
        HalType.BIT: hal_mod.HAL_BIT,
        HalType.FLOAT: hal_mod.HAL_FLOAT,
        HalType.S32: hal_mod.HAL_S32,
        HalType.U32: hal_mod.HAL_U32,
    }[t]


def _hal_dir_constant(hal_mod: Any, d: HalDir) -> int:
    return {
        HalDir.IN: hal_mod.HAL_IN,
        HalDir.OUT: hal_mod.HAL_OUT,
        HalDir.IO: hal_mod.HAL_IO,
    }[d]


def _hal_pin_local_name(name: str, component: str) -> str:
    """Strip the component prefix so `newpin` sees e.g. `dro_x.value-out`."""
    prefix = component + "."
    if name.startswith(prefix):
        return name[len(prefix):]
    return name


def _error_severity(lc: Any, kind: Any) -> ErrorSeverity:
    if kind is None:
        return ErrorSeverity.INFO
    try:
        k = int(kind)
    except (TypeError, ValueError):
        return ErrorSeverity.INFO
    if k == getattr(lc, "OPERATOR_ERROR", 11):
        return ErrorSeverity.OPERATOR_ERROR
    if k == getattr(lc, "OPERATOR_DISPLAY", 13):
        return ErrorSeverity.OPERATOR_DISPLAY
    if k == getattr(lc, "NML_ERROR", 1):
        return ErrorSeverity.NML_ERROR
    return ErrorSeverity.INFO


# ---------------------------------------------------------------------------
# QtcncServer
# ---------------------------------------------------------------------------


class QtcncServer:
    """Daemon orchestrator. Wires stat polling, HAL, and ZMQ transport together."""

    def __init__(
        self,
        endpoint: str,
        *,
        linuxcnc_module: Any,
        hal_module: Any,
        ini_path: Optional[str] = None,
        poll_interval_s: float = 0.05,
        postgui_halfiles: Optional[list[str]] = None,
        halcmd_path: str = "halcmd",
        component_name: str = "qtcnc",
        snapshot_interval_s: float = 30.0,
        sndhwm: int = 10000,
        curve_public_key: Optional[bytes] = None,
        curve_secret_key: Optional[bytes] = None,
        authorized_clients_dir: Optional[str] = None,
        tool_db_path: Optional[str] = None,
        random_toolchanger: bool = False,
    ) -> None:
        self._endpoint = endpoint
        self._linuxcnc = linuxcnc_module
        self._hal = hal_module
        self._ini_path = ini_path
        self._poll_interval_s = poll_interval_s
        self._postgui_halfiles = list(postgui_halfiles or [])
        self._halcmd_path = halcmd_path
        self._component_name = component_name
        self._snapshot_interval_s = snapshot_interval_s
        # 0.0 forces the first poll tick to publish a baseline, giving
        # slow joiners that attach right at daemon startup a reference.
        self._last_snapshot_ts = 0.0
        # Per-process identity returned in WELCOME so a reconnecting
        # client can detect daemon restarts. Regenerated only on process
        # restart (i.e. on every QtcncServer construction).
        self._daemon_instance_id = str(uuid.uuid4())

        self._stat = linuxcnc_module.stat()
        self._command = linuxcnc_module.command()
        self._error_channel = linuxcnc_module.error_channel()
        self._halcomp = hal_module.component(component_name)

        self._hal_pins: dict[str, Any] = {}
        self._hal_pin_specs: dict[str, HalPinSpec] = {}
        self._hal_ready = False
        # Serialize DECLARE_PINS across concurrent clients so two racing
        # first-time declares can't both win.
        self._hal_lock = threading.Lock()
        # Owned IN pins: last-seen values for change detection during polling.
        self._owned_in_pin_values: dict[str, Any] = {}
        # Foreign pins the client has subscribed to: name -> last seen value
        self._subscribed_foreign: dict[str, Any] = {}
        # Line-count cache for the currently-loaded g-code file. Recomputed
        # lazily in _cached_length() and only when the path changes.
        self._cached_length_path: str = ""
        self._cached_length_value: int = 0

        self._coordinates = _load_coordinates(ini_path)

        self._tool_db_path = tool_db_path
        self._tool_db_conn = None
        self._random_toolchanger = random_toolchanger
        if tool_db_path:
            from qtcnc.tools.tooldb_schema import open_db
            self._tool_db_conn = open_db(tool_db_path)

        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        try:
            self._stat.poll()
        except Exception:
            pass
        self._state = stat_to_state_store(
            self._stat, self._linuxcnc, self._coordinates,
        )

        self._transport = ZmqServerTransport(
            endpoint,
            handler=self,
            sndhwm=sndhwm,
            curve_public_key=curve_public_key,
            curve_secret_key=curve_secret_key,
            authorized_clients_dir=authorized_clients_dir,
        )

    # ----- lifecycle -----

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def transport(self) -> ZmqServerTransport:
        return self._transport

    def start(self) -> None:
        if self._poll_thread is not None:
            return
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="qtcnc-poll",
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
        try:
            self._transport.publish_lifecycle(
                Lifecycle.DAEMON_SHUTDOWN, {"endpoint": self._endpoint},
            )
        except Exception:
            pass
        try:
            self._transport.close()
        except Exception:
            pass
        try:
            self._halcomp.exit()
        except Exception:
            pass

    def serve_once(self, timeout_ms: int = -1) -> bool:
        return self._transport.serve_once(timeout_ms)

    def run_forever(self) -> None:
        """Start polling + serve REQ/REP until stopped."""
        self.start()
        try:
            while not self._stop_event.is_set():
                self._transport.serve_once(200)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    # ----- ZmqServerHandler protocol -----

    def on_hello(self) -> dict[str, Any]:
        return {
            "protocol_version": list(PROTOCOL_VERSION),
            "daemon": "qtcnc-serverd",
            "ini_path": self._ini_path or "",
            "component": self._component_name,
            "daemon_instance_id": self._daemon_instance_id,
            "curve_enabled": self._transport.curve_enabled,
            "metrics": {
                "state_diff_dropped": self._transport.state_diff_dropped,
                "snapshot_interval_s": self._snapshot_interval_s,
            },
        }

    def on_get_snapshot(self) -> StateStore:
        with self._state_lock:
            return self._state

    def on_exec_command(self, verb: CommandVerb, kwargs: dict[str, Any]) -> None:
        auto_switch = bool(kwargs.pop("_auto_switch_mode", False))
        with self._state_lock:
            snap = self._state
        try:
            new_state = execute_command(
                snap, self._command, self._linuxcnc, verb, kwargs,
                auto_switch_mode=auto_switch,
            )
        except NackError:
            raise
        except Exception as e:
            raise NackError(f"command {verb} failed: {e}") from e
        if new_state is snap:
            return
        with self._state_lock:
            cur = self._state
            if new_state.machine.motion_mode != cur.machine.motion_mode:
                cur = replace(cur, machine=replace(
                    cur.machine, motion_mode=new_state.machine.motion_mode,
                ))
            if new_state.program.requested_tools != cur.program.requested_tools:
                cur = replace(cur, program=replace(
                    cur.program, requested_tools=new_state.program.requested_tools,
                ))
            self._state = cur

    def on_load_program(self, path: str) -> None:
        import os
        self._transport.publish_lifecycle(
            Lifecycle.PROGRAM_LOADING, {"path": path},
        )
        if path and not os.path.isfile(path):
            self._transport.publish_lifecycle(
                Lifecycle.PROGRAM_MISSING, {"path": path},
            )
            raise NackError(f"program file not found: {path}")
        try:
            self._command.mode(self._linuxcnc.MODE_AUTO)
            self._command.wait_complete()
            self._command.program_open(path)
        except Exception as e:
            self._transport.publish_lifecycle(
                Lifecycle.PROGRAM_LOAD_FAILED,
                {"path": path, "reason": str(e)},
            )
            raise NackError(f"load_program failed: {e}") from e
        total_lines = self._cached_length(path)
        program = ProgramState(
            path=path,
            total_lines=total_lines,
            current_line=0,
            is_running=False,
            is_paused=False,
        )
        with self._state_lock:
            self._state = replace(
                self._state,
                program=replace(self._state.program, requested_tools=frozenset()),
            )
        self._transport.publish_lifecycle(
            Lifecycle.PROGRAM_LOADED,
            {"path": path, "program": program},
        )

    def _cached_length(self, path: str) -> int:
        """Return the line count of `path`, caching by filename.

        Returns 0 for empty/missing paths. The cache holds a single entry;
        loading a new program invalidates the previous count.
        """
        if not path:
            return 0
        if path == self._cached_length_path:
            return self._cached_length_value
        try:
            with open(path, "rb") as f:
                count = sum(1 for _ in f)
        except OSError:
            count = 0
        self._cached_length_path = path
        self._cached_length_value = count
        return count

    def on_declare_pins(self, specs: list[HalPinSpec]) -> DeclarePinsResult:
        with self._hal_lock:
            if self._hal_ready:
                # Subsequent client: every requested spec must match an
                # existing declaration exactly (name + type + dir). This
                # is the "HAL state survives GUI death" inheritance path
                # — the first client owns the HAL surface and anything
                # joining later binds to it read-through.
                for spec in specs:
                    existing = self._hal_pin_specs.get(spec.name)
                    if existing is None:
                        raise NackError("hal_locked")
                    if existing.type != spec.type or existing.dir != spec.dir:
                        raise NackError(f"pin_type_mismatch:{spec.name}")
                return DeclarePinsResult(
                    created=[s.name for s in specs],
                    inherited=True,
                )
            created: list[str] = []
            for spec in specs:
                try:
                    pin = self._halcomp.newpin(
                        _hal_pin_local_name(spec.name, self._component_name),
                        _hal_type_constant(self._hal, spec.type),
                        _hal_dir_constant(self._hal, spec.dir),
                    )
                except Exception as e:
                    raise NackError(f"newpin({spec.name}) failed: {e}") from e
                self._hal_pins[spec.name] = pin
                self._hal_pin_specs[spec.name] = spec
                if spec.initial is not None:
                    try:
                        pin.set(spec.initial)
                    except Exception:
                        pass
                if spec.dir == HalDir.IN:
                    self._owned_in_pin_values[spec.name] = _UNSET
                created.append(spec.name)
            try:
                self._halcomp.ready()
            except Exception as e:
                raise NackError(f"halcomp.ready() failed: {e}") from e
            self._hal_ready = True
        try:
            self._run_postgui_halfiles()
        except subprocess.CalledProcessError as e:
            detail = getattr(e, "output", "") or ""
            self._transport.publish_error(ErrorMessage(
                severity=ErrorSeverity.NML_ERROR,
                text=f"POSTGUI_HALFILE failed: {detail}" if detail else f"POSTGUI_HALFILE failed: {e}",
                timestamp=time.time(),
            ))
        except Exception as e:
            self._transport.publish_error(ErrorMessage(
                severity=ErrorSeverity.NML_ERROR,
                text=f"POSTGUI_HALFILE failed: {e}",
                timestamp=time.time(),
            ))
        self._transport.publish_lifecycle(
            Lifecycle.DAEMON_READY,
            {"component": self._component_name, "pins": created},
        )
        return DeclarePinsResult(created=created, inherited=False)

    def on_write_pin(self, name: str, value: Any) -> None:
        spec = self._hal_pin_specs.get(name)
        if spec is None:
            raise NackError(f"unknown pin: {name}")
        if spec.dir == HalDir.IN:
            raise NackError(f"cannot write IN pin: {name}")
        try:
            self._hal_pins[name].set(value)
        except Exception as e:
            raise NackError(f"pin set failed: {e}") from e
        self._transport.publish_hal_pin(name, value)

    def on_subscribe_pin(self, name: str) -> None:
        # Client asks the daemon to publish updates for a foreign pin.
        # We seed with sentinel None so the first poll publishes the initial value.
        if name not in self._subscribed_foreign:
            self._subscribed_foreign[name] = _UNSET

    def on_get_tool_db(self) -> dict[str, Any]:
        if self._tool_db_conn is None:
            raise NackError("no_tool_db")
        from qtcnc.tools.tooldb_schema import get_all_tools, get_spindle_state
        from qtcnc.core.types import ToolDbEntry
        rows = get_all_tools(self._tool_db_conn)
        entries = []
        for r in rows:
            entries.append(ToolDbEntry(
                tool_id=r["tool_id"], pocket=r["pocket"],
                x_offset=r["x_offset"], y_offset=r["y_offset"],
                z_offset=r["z_offset"], a_offset=r["a_offset"],
                b_offset=r["b_offset"], c_offset=r["c_offset"],
                u_offset=r["u_offset"], v_offset=r["v_offset"],
                w_offset=r["w_offset"], diameter=r["diameter"],
                frontangle=r["frontangle"], backangle=r["backangle"],
                orientation=r["orientation"], comment=r["comment"],
            ))
        return {
            "tools": tuple(entries),
            "spindle_tool_id": get_spindle_state(self._tool_db_conn),
            "random_toolchanger": self._random_toolchanger,
        }

    def on_add_tool(self, tool_id: int, pocket: int, fields: dict[str, Any]) -> None:
        if self._tool_db_conn is None:
            raise NackError("no_tool_db")
        from qtcnc.tools.tooldb_schema import upsert_tool
        upsert_tool(self._tool_db_conn, tool_id, pocket=pocket, **fields)
        self._command.load_tool_table()

    def on_remove_tool(self, tool_id: int) -> None:
        if self._tool_db_conn is None:
            raise NackError("no_tool_db")
        from qtcnc.tools.tooldb_schema import delete_tool
        delete_tool(self._tool_db_conn, tool_id)
        self._command.load_tool_table()

    def on_update_tool(self, tool_id: int, fields: dict[str, Any]) -> None:
        if self._tool_db_conn is None:
            raise NackError("no_tool_db")
        from qtcnc.tools.tooldb_schema import upsert_tool
        upsert_tool(self._tool_db_conn, tool_id, **fields)
        self._command.load_tool_table()

    # ----- polling thread -----

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                _log.error("poll error: %s", e)
            self._stop_event.wait(self._poll_interval_s)

    def _poll_once(self) -> None:
        self._stat.poll()
        new_state = stat_to_state_store(
            self._stat, self._linuxcnc, self._coordinates,
        )
        # Overlay total_lines using the cached line count so the polling
        # hot-path doesn't open the file on every tick.
        length = self._cached_length(new_state.program.path)
        prog_overlay = {}
        if length != new_state.program.total_lines:
            prog_overlay["total_lines"] = length
        # Preserve client-provided requested_tools across polls.
        # stat_to_state_store always returns empty requested_tools (stat
        # doesn't know about them). The overlay unconditionally re-applies
        # whatever the daemon has stored. Clearing happens in
        # on_load_program when a new file is loaded.
        with self._state_lock:
            old_tools = self._state.program.requested_tools
        if old_tools:
            prog_overlay["requested_tools"] = old_tools
        if prog_overlay:
            new_state = replace(
                new_state,
                program=replace(new_state.program, **prog_overlay),
            )
        with self._state_lock:
            old_state = self._state
            self._state = new_state
            changes = diff(old_state, new_state)
        if changes:
            self._transport.publish_state_diff(changes)

        # Periodic baseline snapshot so slow joiners and subscribers that
        # drifted out of sync have a reference point without waiting for
        # every field to change.
        now = time.monotonic()
        if now - self._last_snapshot_ts >= self._snapshot_interval_s:
            with self._state_lock:
                snap = self._state
            self._transport.publish_state_snapshot(snap)
            self._last_snapshot_ts = now

        # Drain error_channel.
        for _ in range(16):  # cap drain per tick to avoid starving stat poll
            err = self._error_channel.poll()
            if not err:
                break
            if isinstance(err, tuple) and len(err) == 2:
                kind, text = err
            else:
                kind, text = None, str(err)
            self._transport.publish_error(ErrorMessage(
                severity=_error_severity(self._linuxcnc, kind),
                text=str(text),
                timestamp=time.time(),
            ))

        # Owned IN pin polling — reads pins written by other HAL
        # components (e.g. iocontrol.0.tool-change netted to
        # qtcnc.manual-tool-change.change) and publishes changes.
        for name, last in list(self._owned_in_pin_values.items()):
            pin = self._hal_pins.get(name)
            if pin is None:
                continue
            try:
                value = pin.get()
            except Exception:
                continue
            if value != last:
                self._owned_in_pin_values[name] = value
                self._transport.publish_hal_pin(name, value)

        # Foreign pin polling.
        if self._subscribed_foreign:
            getter = getattr(self._hal, "get_value", None)
            if getter is not None:
                for name in list(self._subscribed_foreign.keys()):
                    try:
                        value = getter(name)
                    except Exception:
                        continue
                    if self._subscribed_foreign[name] != value:
                        self._subscribed_foreign[name] = value
                        self._transport.publish_hal_pin(name, value)

    # ----- POSTGUI halfile execution -----

    def _run_postgui_halfiles(self) -> None:
        import os
        ini_dir = os.path.dirname(os.path.abspath(self._ini_path)) if self._ini_path else ""
        for f in self._postgui_halfiles:
            path = os.path.expanduser(f)
            if not os.path.isabs(path) and ini_dir:
                path = os.path.join(ini_dir, path)
            if path.lower().endswith(".tcl"):
                argv = ["haltcl"]
                if self._ini_path:
                    argv += ["-i", self._ini_path]
                argv.append(path)
            else:
                argv = [self._halcmd_path]
                if self._ini_path:
                    argv += ["-i", self._ini_path]
                argv += ["-f", path]
            result = subprocess.run(
                argv, check=False, timeout=60,
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise subprocess.CalledProcessError(
                    result.returncode, argv,
                    output=f"halcmd failed: {detail}",
                )


_UNSET = object()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _load_coordinates(ini_path: Optional[str]) -> tuple[str, ...]:
    """Read [TRAJ]COORDINATES from the INI. Returns a per-joint axis letter
    tuple, e.g. ("X", "Y", "Z") or ("X", "Y", "Y", "Z") for a gantry.
    """
    if not ini_path:
        return ()
    import configparser
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(ini_path)
    raw = cp.get("TRAJ", "COORDINATES", fallback="")
    return tuple(c.upper() for c in raw if c.isalpha())


def _load_postgui_halfiles(ini_path: Optional[str]) -> list[str]:
    if not ini_path:
        return []
    import configparser
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(ini_path)
    files: list[str] = []
    if cp.has_section("HAL"):
        # POSTGUI_HALFILE may appear multiple times; configparser only keeps
        # the last one. Fall back to reading the file for duplicates if needed.
        value = cp.get("HAL", "POSTGUI_HALFILE", fallback="")
        for f in value.split():
            if f:
                files.append(f)
    return files


def _load_tool_db_config(
    ini_path: Optional[str],
) -> tuple[Optional[str], bool]:
    """Read [QTCNC]TOOL_DB and [EMCIO]RANDOM_TOOLCHANGER from the INI.

    Returns (tool_db_path, random_toolchanger). Relative TOOL_DB paths
    are resolved against the INI file's directory.
    """
    if not ini_path:
        return None, False
    import configparser
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(ini_path)
    ini_dir = os.path.dirname(os.path.abspath(ini_path))

    tool_db_raw = cp.get("QTCNC", "TOOL_DB", fallback=None)
    tool_db_path: Optional[str] = None
    if tool_db_raw is not None:
        tool_db_raw = tool_db_raw.strip()
        if os.path.isabs(tool_db_raw):
            tool_db_path = tool_db_raw
        else:
            tool_db_path = os.path.join(ini_dir, tool_db_raw)

    random_tc_raw = cp.get("EMCIO", "RANDOM_TOOLCHANGER", fallback="0")
    try:
        random_tc = int(random_tc_raw.strip()) != 0
    except (ValueError, AttributeError):
        random_tc = False

    return tool_db_path, random_tc


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="qtcnc-serverd",
        description="qtcnc daemon: owns linuxcnc.stat/command and hal.component('qtcnc')",
    )
    p.add_argument("--ini", "-ini", dest="ini_path", default=None,
                   help="LinuxCNC INI file")
    p.add_argument("--endpoint", default=None,
                   help="ZMQ base endpoint (default: ipc:///tmp/qtcnc-<uid>)")
    p.add_argument("--component-name", default="qtcnc",
                   help="HAL component name (default: qtcnc)")
    p.add_argument("--poll-hz", type=float, default=20.0,
                   help="stat poll rate (default: 20 Hz)")
    p.add_argument("--curve-secret-key", default=None,
                   help="path to a CURVE *.key_secret file (enables encryption)")
    p.add_argument("--authorized-clients-dir", default=None,
                   help="directory of *.key public certs (allow-listed clients); "
                        "if omitted with --curve-secret-key, any encrypted client is accepted")
    return p.parse_args(argv)


def _load_curve_keypair(path: str) -> tuple[bytes, bytes]:
    """Load a CURVE keypair from a `.key_secret` file. Returns (public, secret)."""
    from zmq.auth.certs import load_certificate
    pub, sec = load_certificate(path)
    if pub is None or sec is None:
        raise ValueError(
            f"{path} does not contain both public and secret keys"
        )
    return pub, sec


def main(argv: Optional[list[str]] = None) -> int:
    import signal
    from qtcnc.logging_setup import setup_logging
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging()

    import linuxcnc as linuxcnc_module  # type: ignore
    import hal as hal_module  # type: ignore

    if args.endpoint:
        endpoint = args.endpoint
    else:
        endpoint = f"ipc:///tmp/qtcnc-{os.getuid()}"

    postgui = _load_postgui_halfiles(args.ini_path)
    tool_db_path, random_tc = _load_tool_db_config(args.ini_path)

    curve_pub: Optional[bytes] = None
    curve_sec: Optional[bytes] = None
    if args.curve_secret_key:
        curve_pub, curve_sec = _load_curve_keypair(args.curve_secret_key)

    server = QtcncServer(
        endpoint,
        linuxcnc_module=linuxcnc_module,
        hal_module=hal_module,
        ini_path=args.ini_path,
        poll_interval_s=1.0 / max(args.poll_hz, 1.0),
        postgui_halfiles=postgui,
        component_name=args.component_name,
        curve_public_key=curve_pub,
        curve_secret_key=curve_sec,
        authorized_clients_dir=args.authorized_clients_dir,
        tool_db_path=tool_db_path,
        random_toolchanger=random_tc,
    )

    # When the launcher (or systemd) sends SIGTERM, drop into the same
    # graceful shutdown path as Ctrl-C: set _stop_event, let serve_once
    # return, run finally: stop().
    def _on_term(signum, frame):
        server._stop_event.set()

    signal.signal(signal.SIGTERM, _on_term)

    try:
        server.run_forever()
    except Exception as e:
        _log.critical("fatal: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
