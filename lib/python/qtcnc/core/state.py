"""StateStore: the single snapshot of machine state + diff/apply.

diff() walks every top-level field of StateStore and emits a list of
(field_name, new_value) tuples for fields that changed. apply() consumes
such a list to produce a new StateStore. No Qt imports — fully testable
without a QApplication.

Design choice: diffs are top-level, not recursive into nested dataclasses.
When the Position changes (9 floats), the whole Position flies across the
wire. At ~40Hz with msgpack this is trivial, and the code stays simple.
Per-axis signals are emitted on the client side by comparing the old and
new Position, not by sending per-axis diffs.

State predicates
----------------
StateStore exposes composite @property predicates that consult both
MachineState and ProgramState. These are the canonical gates widgets
and the daemon consult before issuing commands that span both state
layers (program run, MDI, program load). Primitive predicates live on
the sub-dataclasses in `qtcnc.core.types`.

Composites:
    can_run_program, can_resume_program, can_pause_program,
    can_abort_program, can_step_program, can_mdi, can_load_program,
    can_edit_program

Top-level field groups
----------------------
Core motion/program state lives in dedicated top-level fields
(`machine`, `position`, `program`, `tool`, `spindles`, `overrides`).
Every other slice of `linuxcnc.stat()` is grouped into a sub-dataclass
from `qtcnc.core.types` and attached as a StateStore field:

    task_info           task daemon bookkeeping
    offsets             g5x/g92/tool offsets + XY rotation
    active_settings     interpreter 5-tuple (seq, feed, speed, tolerances)
    joints              per-joint tuple (trimmed to machine.joint_count)
    axes                per-axis tuple (trimmed by axis_mask)
    tool_table          full tool-table rows
    coolant             mist + flood
    probe               probe_tripped/probing/value/probed_position
    io                  digital/analog IO + pocket + aux estop
    commanded_position  commanded (trajectory-planner) position
    heartbeat           motion controller heartbeat counter
    taskbeat            task main-loop heartbeat counter
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any

from qtcnc.core.types import (
    AxisState,
    CoolantState,
    InterpSettings,
    IoState,
    JointState,
    MachineState,
    Offsets,
    Overrides,
    Position,
    ProbeState,
    ProgramState,
    SpindleState,
    TaskInfo,
    TaskState,
    Tool,
    ToolEntry,
)


@dataclass(frozen=True, slots=True)
class StateStore:
    """Complete machine snapshot. Immutable; diffs produce new instances."""

    connected: bool = False
    task_state: TaskState = TaskState.ESTOP
    machine: MachineState = field(default_factory=MachineState)
    position: Position = field(default_factory=Position)
    machine_position: Position = field(default_factory=Position)
    dtg: Position = field(default_factory=Position)
    program: ProgramState = field(default_factory=ProgramState)
    tool: Tool = field(default_factory=Tool)
    tool_in_spindle: int = 0
    spindles: tuple[SpindleState, ...] = field(default_factory=lambda: (SpindleState(),))
    overrides: Overrides = field(default_factory=Overrides)
    feed_rate: float = 0.0
    rapid_rate: float = 0.0
    active_gcodes: tuple[int, ...] = ()
    active_mcodes: tuple[int, ...] = ()
    # --- task/interpreter bookkeeping ---
    task_info: TaskInfo = field(default_factory=TaskInfo)
    offsets: Offsets = field(default_factory=Offsets)
    active_settings: InterpSettings = field(default_factory=InterpSettings)
    # --- per-joint / per-axis tuples, trimmed to active count ---
    joints: tuple[JointState, ...] = ()
    axes: tuple[AxisState, ...] = ()
    # --- tool table ---
    tool_table: tuple[ToolEntry, ...] = ()
    # --- I/O, coolant, probe ---
    coolant: CoolantState = field(default_factory=CoolantState)
    probe: ProbeState = field(default_factory=ProbeState)
    io: IoState = field(default_factory=IoState)
    # --- commanded (trajectory-planner) position, distinct from actual_position ---
    commanded_position: Position = field(default_factory=Position)
    # --- raw heartbeats for daemon health visibility ---
    heartbeat: int = 0
    taskbeat: int = 0

    @property
    def can_run_program(self) -> bool:
        """A program run is permitted when a file is loaded and not
        already running, the machine is ready, AUTO is selected, the
        interpreter is idle, and every tool the program references
        exists in the tool table.
        """
        if not (
            self.program.is_runnable
            and self.machine.is_ready
            and self.machine.is_all_homed
            and self.machine.is_auto
            and self.machine.is_idle
        ):
            return False
        if self.program.requested_tools:
            known = {t.id for t in self.tool_table if t.id > 0}
            if not self.program.requested_tools <= known:
                return False
        return True

    @property
    def missing_tools(self) -> frozenset[int]:
        """Tool numbers the loaded program requests that are absent
        from the current tool table. Empty when no program is loaded
        or all tools are present.
        """
        if not self.program.requested_tools:
            return frozenset()
        known = {t.id for t in self.tool_table if t.id > 0}
        missing = self.program.requested_tools - known
        return frozenset(missing) if missing else frozenset()

    @property
    def can_resume_program(self) -> bool:
        """Resume is meaningful only while a program is paused and the
        machine is ready in AUTO.
        """
        return (
            self.program.is_paused
            and self.machine.is_ready
            and self.machine.is_auto
        )

    @property
    def can_pause_program(self) -> bool:
        """Pause is meaningful only while a program is running and not
        already paused.
        """
        return self.program.is_running and not self.program.is_paused

    @property
    def can_abort_program(self) -> bool:
        """Abort is allowed whenever the machine is ready; the
        controller treats it as a safe no-op when nothing is running.
        """
        return self.machine.is_ready

    @property
    def can_step_program(self) -> bool:
        """Single-step is available in AUTO on a ready machine when a
        program is loaded or currently paused.
        """
        return (
            self.machine.is_ready
            and self.machine.is_auto
            and (self.program.has_program or self.program.is_paused)
        )

    @property
    def can_mdi(self) -> bool:
        """MDI entry is allowed when the machine is ready, MDI mode is
        selected, and the interpreter is idle.
        """
        return (
            self.machine.is_ready
            and self.machine.is_mdi
            and self.machine.is_idle
        )

    @property
    def can_load_program(self) -> bool:
        """A new program may be loaded only when the currently-loaded
        one (if any) is not mid-execution. Drive state is not checked:
        the load target is the interpreter, not the motors.
        """
        return self.program.is_editable

    @property
    def can_edit_program(self) -> bool:
        """Readable alias for `program.is_editable` at widget call sites."""
        return self.program.is_editable

    @property
    def is_any_homing(self) -> bool:
        """True when at least one joint is mid-homing-sequence."""
        return any(j.is_homing for j in self.joints)


def diff(old: StateStore, new: StateStore) -> list[tuple[str, Any]]:
    """Return a list of (field_name, new_value) tuples for changed fields.

    Only compares top-level fields. Two StateStores with identical fields
    produce an empty list. Field order follows the dataclass definition.
    """
    changes: list[tuple[str, Any]] = []
    for f in fields(StateStore):
        old_value = getattr(old, f.name)
        new_value = getattr(new, f.name)
        if old_value != new_value:
            changes.append((f.name, new_value))
    return changes


def apply(store: StateStore, changes: list[tuple[str, Any]]) -> StateStore:
    """Return a new StateStore with `changes` applied.

    Unknown field names in `changes` raise TypeError via dataclasses.replace.
    """
    if not changes:
        return store
    return replace(store, **dict(changes))
