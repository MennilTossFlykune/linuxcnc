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
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any

from qtcnc.core.types import (
    MachineState,
    Overrides,
    Position,
    ProgramState,
    SpindleState,
    TaskState,
    Tool,
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
