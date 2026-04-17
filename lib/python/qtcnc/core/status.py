"""Status: client-side StateStore owner and signal hub.

`Status` is a QObject that:

1. Owns the local `StateStore`.
2. Registers as the Transport's callback target for state diffs, errors,
   lifecycle events, and connection state transitions.
3. Translates those callbacks into ~40 typed Qt signals that widgets and
   handlers subscribe to.

The Transport is responsible for threading: in the ZMQ implementation,
the SUB thread marshals every message through a queued Qt signal so by
the time Status sees a callback it is already on the main thread. In
mock mode the callback runs synchronously in the caller's thread. Either
way, Status mutates its StateStore and emits signals without any
locking of its own.

Signal design rules:

* Signals carry typed values, never raw tuples. `position_changed(Position)`,
  not `position_changed(float, float, float)`.
* Dataclasses and enums travel as `Signal(object)` since Qt can't
  introspect Python types for metaobject registration.
* Per-axis / per-spindle signals (`axis_position_changed(int, float)`,
  `spindle_speed_changed(int, float)`) exist alongside the umbrella
  signals so single-axis DROs and per-spindle displays don't have to
  do their own diffing.
"""

from __future__ import annotations

import os.path
import time
from dataclasses import fields, replace
from typing import Any, Callable

from qtpy.QtCore import QObject, Signal

from qtcnc.core.state import StateStore, apply, diff
from qtcnc.core.types import (
    ErrorMessage,
    ErrorSeverity,
    InterpState,
    JointState,
    MachineState,
    Message,
    MessageSeverity,
    MessageSource,
    Overrides,
    Position,
    ProgramState,
    SpindleState,
    TaskState,
)
from qtcnc.signals import Lifecycle
from qtcnc.transport.base import Transport


# Map of axis index -> Position field name.
_AXIS_FIELDS: tuple[str, ...] = ("x", "y", "z", "a", "b", "c", "u", "v", "w")


class Status(QObject):
    """Qt-side client state. Register callbacks on the Transport, fan out to signals."""

    # --- Connection ---
    connected = Signal()
    disconnected = Signal(str)
    reconnecting = Signal(int)
    daemon_restarted = Signal()
    version_mismatch = Signal(str, str)

    # --- Machine state ---
    estop_changed = Signal(bool)
    power_changed = Signal(bool)
    task_state_changed = Signal(object)        # TaskState
    task_mode_changed = Signal(object)         # TaskMode
    interp_state_changed = Signal(object)      # InterpState
    motion_type_changed = Signal(object)       # MotionType
    homed_changed = Signal(int, bool)
    all_homed_changed = Signal(bool)
    homing_changed = Signal(int, bool)         # (joint, is_homing)
    any_homing_changed = Signal(bool)
    machine_state_changed = Signal(object)     # MachineState umbrella

    # --- Position ---
    position_changed = Signal(object)          # Position (work)
    machine_position_changed = Signal(object)  # Position (absolute)
    dtg_changed = Signal(object)               # Position (distance-to-go)
    axis_position_changed = Signal(int, float)

    # --- Program lifecycle ---
    program_loading = Signal(str)
    program_loaded = Signal(str, object)       # path, ProgramState
    program_load_failed = Signal(str, str)
    program_missing = Signal(str)
    program_closed = Signal()
    program_started = Signal()
    program_paused = Signal()
    program_finished = Signal(bool)            # success
    program_line_changed = Signal(int)         # ProgramState.current_line (interpreter read-ahead)
    motion_line_changed = Signal(int)          # ProgramState.motion_line (line being executed)
    program_tools_changed = Signal(object)     # ProgramState.requested_tools (frozenset[int])

    # --- Tool / spindle / feed ---
    tool_changed = Signal(object)              # Tool
    tool_in_spindle_changed = Signal(int)
    tool_table_changed = Signal(object)        # tuple[ToolEntry, ...]
    spindle_speed_changed = Signal(int, float)
    spindle_direction_changed = Signal(int, object)  # (index, SpindleDir)
    feed_rate_changed = Signal(float)
    rapid_rate_changed = Signal(float)

    # --- Overrides ---
    feed_override_changed = Signal(float)
    rapid_override_changed = Signal(float)
    spindle_override_changed = Signal(int, float)
    overrides_changed = Signal(object)         # Overrides umbrella

    # --- Coolant ---
    coolant_changed = Signal(object)           # CoolantState

    # --- Task / interpreter bookkeeping ---
    task_info_changed = Signal(object)         # TaskInfo umbrella
    spindles_changed = Signal(object)          # tuple[SpindleState, ...] umbrella
    offsets_changed = Signal(object)           # Offsets umbrella

    # --- Active codes + errors ---
    active_gcodes_changed = Signal(object)     # tuple[int, ...]
    active_mcodes_changed = Signal(object)     # tuple[int, ...]
    error = Signal(object, str)                # ErrorSeverity, text
    message = Signal(object)                   # Message

    # ----- construction -----

    def __init__(self, transport: Transport, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._transport = transport
        self._state: StateStore = StateStore()
        self._wire_callbacks()

    def _wire_callbacks(self) -> None:
        self._transport.set_on_state_diff(self._on_state_diff)
        self._transport.set_on_error(self._on_error)
        self._transport.set_on_lifecycle(self._on_lifecycle)
        self._transport.set_on_connected(self._on_connected)
        self._transport.set_on_disconnected(self._on_disconnected)

    # ----- public API -----

    @property
    def state(self) -> StateStore:
        return self._state

    def bootstrap(self) -> None:
        """Pull the initial snapshot from the transport and fan out signals.

        Call this after `transport.hello()` has succeeded and before
        `DECLARE_PINS`. Bootstrapping compares the default StateStore to
        the snapshot so signals fire for every non-default field.
        """
        snap = self._transport.get_snapshot()
        changes = diff(self._state, snap)
        # Skip the `connected` field: that lifecycle is already handled by
        # _on_connected which the transport fires from hello().
        changes = [(n, v) for (n, v) in changes if n != "connected"]
        old = self._state
        self._state = replace(snap, connected=self._state.connected)
        self._emit_changes(old, changes)

    # ----- callback handlers -----

    def _on_connected(self) -> None:
        if not self._state.connected:
            self._state = replace(self._state, connected=True)
        self.connected.emit()
        _post_info(self, "Connected to daemon")

    def _on_disconnected(self, reason: str) -> None:
        if self._state.connected:
            self._state = replace(self._state, connected=False)
        self.disconnected.emit(reason)
        self.message.emit(Message(
            severity=MessageSeverity.WARNING, source=MessageSource.FRAMEWORK,
            text=f"Disconnected: {reason}", timestamp=time.time(),
        ))

    def _on_error(self, err: ErrorMessage) -> None:
        self.error.emit(err.severity, err.text)
        if err.severity in (ErrorSeverity.OPERATOR_ERROR, ErrorSeverity.NML_ERROR):
            sev = MessageSeverity.ERROR
        else:
            sev = MessageSeverity.INFO
        self.message.emit(Message(
            severity=sev, source=MessageSource.LINUXCNC,
            text=err.text, timestamp=err.timestamp,
        ))

    def _on_lifecycle(self, tag: str, payload: dict[str, Any]) -> None:
        if tag == Lifecycle.PROGRAM_LOADING:
            path = payload.get("path", "")
            self.program_loading.emit(path)
            _post_info(self, f"Loading {_basename(path)}")
        elif tag == Lifecycle.PROGRAM_LOADED:
            path = payload.get("path", "")
            raw = payload.get("program")
            # Over the wire the daemon's ProgramState arrives as a plain
            # dict (`to_wire(dataclass)`). MockTransport dispatches the
            # dataclass instance directly. Accept either and always hand
            # the slot a typed ProgramState so handlers can use attribute
            # access without discovering the underlying transport.
            if isinstance(raw, ProgramState):
                ps = raw
            elif isinstance(raw, dict):
                ps = ProgramState(**{
                    k: v for k, v in raw.items()
                    if k in {"path", "total_lines", "current_line",
                             "is_running", "is_paused"}
                })
            else:
                ps = ProgramState(path=path)
            self.program_loaded.emit(path, ps)
            _post_info(self, f"Loaded {_basename(path)}")
        elif tag == Lifecycle.PROGRAM_LOAD_FAILED:
            path = payload.get("path", "")
            reason = payload.get("reason", "")
            self.program_load_failed.emit(path, reason)
            self.message.emit(Message(
                severity=MessageSeverity.WARNING, source=MessageSource.FRAMEWORK,
                text=f"Failed to load {_basename(path)}: {reason}",
                timestamp=time.time(),
            ))
        elif tag == Lifecycle.PROGRAM_MISSING:
            path = payload.get("path", "")
            self.program_missing.emit(path)
            self.message.emit(Message(
                severity=MessageSeverity.WARNING, source=MessageSource.FRAMEWORK,
                text=f"File not found: {_basename(path)}",
                timestamp=time.time(),
            ))
        elif tag == Lifecycle.PROGRAM_CLOSED:
            self.program_closed.emit()
        elif tag == Lifecycle.PROGRAM_FINISHED:
            self.program_finished.emit(bool(payload.get("success", True)))
        # DAEMON_READY / DAEMON_SHUTDOWN / PROGRAM_STARTED / PROGRAM_PAUSED
        # are not wired here: started/paused come from state_diff (is_running/
        # is_paused flag transitions), daemon_* are handled by the reconnector.

    def _on_state_diff(self, changes: list[tuple[str, Any]]) -> None:
        old = self._state
        self._state = apply(old, changes)
        self._emit_changes(old, changes)

    # ----- Reconnector integration -----

    def attach_reconnector(self, reconnector: "Any") -> None:
        """Wire a Reconnector's signals into Status's connection signals.

        The Reconnector owns the PING-loop state machine; Status owns the
        public-facing Qt signals widgets and handlers care about. This
        method bridges the two so widgets only have to subscribe to
        `Status.disconnected/reconnecting/daemon_restarted` and not learn
        about the Reconnector type.
        """
        reconnector.state_changed.connect(self._on_reconnector_state)
        reconnector.reconnect_attempt.connect(self.reconnecting)
        reconnector.daemon_restarted.connect(self.daemon_restarted)

    def _on_reconnector_state(self, state: str) -> None:
        if state == "DISCONNECTED":
            # Reconnector decided the link is dead. Push the state through
            # the same path the Transport callbacks would use so widgets see
            # one canonical disconnect event.
            self._on_disconnected("link lost")
        # CONNECTED / RECONNECTING are surfaced via the explicit signals
        # already wired through `connect()` above (transport.hello fires
        # _on_connected for CONNECTED; reconnect_attempt fires reconnecting).

    # ----- signal fan-out -----

    def _emit_changes(self, old: StateStore, changes: list[tuple[str, Any]]) -> None:
        for name, new_value in changes:
            handler = _FIELD_HANDLERS.get(name)
            if handler is not None:
                handler(self, old, new_value)


# ---------------------------------------------------------------------------
# Per-field handlers
#
# Each function is called with (status, old_store, new_field_value) and is
# responsible for emitting all signals that follow from the change. They are
# module-level functions (not methods) so the mapping can be declared as a
# simple dict without extra reflection.
# ---------------------------------------------------------------------------


def _basename(path: str) -> str:
    return os.path.basename(path) if path else path


def _post_info(status: Status, text: str) -> None:
    """Emit a framework-level INFO message for common state transitions."""
    status.message.emit(Message(
        severity=MessageSeverity.INFO, source=MessageSource.FRAMEWORK,
        text=text, timestamp=time.time(),
    ))


def _noop(status: Status, old: StateStore, new_value: Any) -> None:
    pass


def _h_task_state(status: Status, old: StateStore, new_value: TaskState) -> None:
    status.task_state_changed.emit(new_value)


def _h_machine(status: Status, old: StateStore, new_value: MachineState) -> None:
    m_old = old.machine
    m_new = new_value
    if m_old.estop != m_new.estop:
        status.estop_changed.emit(m_new.estop)
        _post_info(status, "E-stop asserted" if m_new.estop else "E-stop reset")
    if m_old.powered != m_new.powered:
        status.power_changed.emit(m_new.powered)
        _post_info(status, "Machine powered on" if m_new.powered else "Machine powered off")
    if m_old.task_mode != m_new.task_mode:
        status.task_mode_changed.emit(m_new.task_mode)
        _post_info(status, f"Mode: {m_new.task_mode.name}")
    if m_old.interp_state != m_new.interp_state:
        status.interp_state_changed.emit(m_new.interp_state)
    if m_old.motion_type != m_new.motion_type:
        status.motion_type_changed.emit(m_new.motion_type)
    if m_old.homed != m_new.homed:
        max_len = max(len(m_old.homed), len(m_new.homed))
        for axis in range(max_len):
            old_val = m_old.homed[axis] if axis < len(m_old.homed) else False
            new_val = m_new.homed[axis] if axis < len(m_new.homed) else False
            if old_val != new_val:
                status.homed_changed.emit(axis, new_val)
        if m_old.is_all_homed != m_new.is_all_homed:
            status.all_homed_changed.emit(m_new.is_all_homed)
            if m_new.is_all_homed:
                _post_info(status, "All axes homed")
    status.machine_state_changed.emit(m_new)


def _h_position(status: Status, old: StateStore, new_value: Position) -> None:
    p_old = old.position
    p_new = new_value
    for idx, field in enumerate(_AXIS_FIELDS):
        old_axis = getattr(p_old, field)
        new_axis = getattr(p_new, field)
        if old_axis != new_axis and new_axis is not None:
            status.axis_position_changed.emit(idx, float(new_axis))
    status.position_changed.emit(p_new)


def _h_machine_position(status: Status, old: StateStore, new_value: Position) -> None:
    status.machine_position_changed.emit(new_value)


def _h_dtg(status: Status, old: StateStore, new_value: Position) -> None:
    status.dtg_changed.emit(new_value)


def _h_program(status: Status, old: StateStore, new_value: ProgramState) -> None:
    p_old = old.program
    p_new = new_value
    # program_started: transition from fully idle (not running, not paused)
    # into running. A resume from paused is NOT a fresh start, so we require
    # p_old to have been unpaused too.
    if (
        not p_old.is_running
        and p_new.is_running
        and not p_old.is_paused
        and not p_new.is_paused
    ):
        status.program_started.emit()
        _post_info(status, "Program running")
    if not p_old.is_paused and p_new.is_paused:
        status.program_paused.emit()
        _post_info(status, "Program paused")
    # program_finished: transition out of running without entering pause.
    # Success is inferred from the current interp_state: IDLE means a clean
    # end; anything else (e.g. the machine went to estop mid-run) is reported
    # as failure so UIs can distinguish "job done" from "job aborted".
    if p_old.is_running and not p_new.is_running and not p_new.is_paused:
        interp_new = status._state.machine.interp_state
        success = interp_new == InterpState.IDLE
        status.program_finished.emit(success)
        _post_info(status, "Program completed" if success else "Program aborted")
    if p_old.current_line != p_new.current_line:
        status.program_line_changed.emit(int(p_new.current_line))
    if p_old.motion_line != p_new.motion_line:
        status.motion_line_changed.emit(int(p_new.motion_line))
    if p_old.requested_tools != p_new.requested_tools:
        status.program_tools_changed.emit(p_new.requested_tools)


def _h_tool(status: Status, old: StateStore, new_value: Any) -> None:
    status.tool_changed.emit(new_value)


def _h_tool_in_spindle(status: Status, old: StateStore, new_value: int) -> None:
    status.tool_in_spindle_changed.emit(new_value)


def _h_tool_table(status: Status, old: StateStore, new_value: Any) -> None:
    status.tool_table_changed.emit(new_value)


def _h_spindles(
    status: Status, old: StateStore, new_value: tuple[SpindleState, ...]
) -> None:
    for idx, new_sp in enumerate(new_value):
        old_sp = old.spindles[idx] if idx < len(old.spindles) else SpindleState(index=idx)
        if old_sp.speed != new_sp.speed:
            status.spindle_speed_changed.emit(idx, new_sp.speed)
        if old_sp.direction != new_sp.direction:
            status.spindle_direction_changed.emit(idx, new_sp.direction)
    status.spindles_changed.emit(new_value)


def _h_overrides(status: Status, old: StateStore, new_value: Overrides) -> None:
    o_old = old.overrides
    o_new = new_value
    if o_old.feed != o_new.feed:
        status.feed_override_changed.emit(o_new.feed)
    if o_old.rapid != o_new.rapid:
        status.rapid_override_changed.emit(o_new.rapid)
    if o_old.spindles != o_new.spindles:
        for idx, val in enumerate(o_new.spindles):
            old_val = o_old.spindles[idx] if idx < len(o_old.spindles) else 1.0
            if old_val != val:
                status.spindle_override_changed.emit(idx, val)
    status.overrides_changed.emit(o_new)


def _h_coolant(status: Status, old: StateStore, new_value: Any) -> None:
    status.coolant_changed.emit(new_value)


def _h_task_info(status: Status, old: StateStore, new_value: Any) -> None:
    status.task_info_changed.emit(new_value)


def _h_offsets(status: Status, old: StateStore, new_value: Any) -> None:
    status.offsets_changed.emit(new_value)


def _h_feed_rate(status: Status, old: StateStore, new_value: float) -> None:
    status.feed_rate_changed.emit(new_value)


def _h_rapid_rate(status: Status, old: StateStore, new_value: float) -> None:
    status.rapid_rate_changed.emit(new_value)


def _h_active_gcodes(status: Status, old: StateStore, new_value: tuple[int, ...]) -> None:
    status.active_gcodes_changed.emit(new_value)


def _h_active_mcodes(status: Status, old: StateStore, new_value: tuple[int, ...]) -> None:
    status.active_mcodes_changed.emit(new_value)


def _h_joints(
    status: Status, old: StateStore, new_value: tuple[JointState, ...]
) -> None:
    old_joints = old.joints
    old_any = any(j.is_homing for j in old_joints)
    new_any = any(j.is_homing for j in new_value)
    for idx, new_j in enumerate(new_value):
        old_j = old_joints[idx] if idx < len(old_joints) else JointState()
        if old_j.is_homing != new_j.is_homing:
            status.homing_changed.emit(idx, new_j.is_homing)
    if old_any != new_any:
        status.any_homing_changed.emit(new_any)
        if new_any:
            _post_info(status, "Homing in progress")
        else:
            _post_info(status, "Homing complete")


_FIELD_HANDLERS: dict[str, Callable[[Status, StateStore, Any], None]] = {
    "connected": _noop,  # handled by on_connected/_on_disconnected
    "task_state": _h_task_state,
    "machine": _h_machine,
    "position": _h_position,
    "machine_position": _h_machine_position,
    "dtg": _h_dtg,
    "program": _h_program,
    "tool": _h_tool,
    "tool_in_spindle": _h_tool_in_spindle,
    "spindles": _h_spindles,
    "overrides": _h_overrides,
    "feed_rate": _h_feed_rate,
    "rapid_rate": _h_rapid_rate,
    "active_gcodes": _h_active_gcodes,
    "active_mcodes": _h_active_mcodes,
    # Full `linuxcnc.stat()` coverage lands on StateStore but dedicated Qt
    # signals for each field are wired only when a widget consumes them.
    # Widgets can still read these fields directly from `status.state.*`.
    "task_info": _h_task_info,
    "offsets": _h_offsets,
    "active_settings": _noop,
    "joints": _h_joints,
    "axes": _noop,
    "tool_table": _h_tool_table,
    "coolant": _h_coolant,
    "probe": _noop,
    "io": _noop,
    "commanded_position": _noop,
    "heartbeat": _noop,
    "taskbeat": _noop,
}


# Sanity check: every StateStore field must be represented in _FIELD_HANDLERS
# so a diff on any field routes somewhere. Runs at import time.
def _check_handler_coverage() -> None:
    missing = {f.name for f in fields(StateStore)} - set(_FIELD_HANDLERS)
    if missing:
        raise RuntimeError(
            f"Status missing signal handlers for StateStore fields: {sorted(missing)}"
        )


_check_handler_coverage()
