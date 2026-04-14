"""Value types for the qtcnc framework.

All types are frozen+slots dataclasses. Signals emit new instances rather
than mutating in place — identity equals value. Stdlib only; safe to import
from Qt Designer plugin loaders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class TaskState(IntEnum):
    ESTOP = 1
    ESTOP_RESET = 2
    OFF = 3
    ON = 4


class TaskMode(IntEnum):
    MANUAL = 1
    AUTO = 2
    MDI = 3


class InterpState(IntEnum):
    IDLE = 1
    READING = 2
    PAUSED = 3
    WAITING = 4


class MotionType(IntEnum):
    NONE = 0
    TRAVERSE = 1
    FEED = 2
    ARC = 3
    TOOLCHANGE = 4
    PROBING = 5
    INDEX_ROTARY = 6


class SpindleDir(IntEnum):
    REVERSE = -1
    STOP = 0
    FORWARD = 1


class ErrorSeverity(IntEnum):
    INFO = 0
    OPERATOR_DISPLAY = 1
    OPERATOR_ERROR = 2
    NML_ERROR = 3


@dataclass(frozen=True, slots=True)
class Position:
    """Nine-axis position. Missing axes are None."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    a: float | None = None
    b: float | None = None
    c: float | None = None
    u: float | None = None
    v: float | None = None
    w: float | None = None


@dataclass(frozen=True, slots=True)
class MachineState:
    estop: bool = True
    powered: bool = False
    task_mode: TaskMode = TaskMode.MANUAL
    interp_state: InterpState = InterpState.IDLE
    motion_type: MotionType = MotionType.NONE
    homed: tuple[bool, ...] = ()
    axis_count: int = 3


@dataclass(frozen=True, slots=True)
class ProgramState:
    path: str = ""
    total_lines: int = 0
    current_line: int = 0
    is_running: bool = False
    is_paused: bool = False


@dataclass(frozen=True, slots=True)
class Tool:
    id: int = 0
    pocket: int = 0
    offset: Position = field(default_factory=Position)
    diameter: float = 0.0
    comment: str = ""


@dataclass(frozen=True, slots=True)
class SpindleState:
    index: int = 0
    speed: float = 0.0
    direction: SpindleDir = SpindleDir.STOP
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class Overrides:
    feed: float = 1.0
    rapid: float = 1.0
    spindles: tuple[float, ...] = (1.0,)


@dataclass(frozen=True, slots=True)
class ErrorMessage:
    severity: ErrorSeverity
    text: str
    timestamp: float


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Generic OS-level device (e.g., a USB stick)."""

    node: str
    subsystem: str
    dev_type: str
    vendor: str = ""
    model: str = ""
    serial: str = ""


@dataclass(frozen=True, slots=True)
class MountInfo:
    source: str
    target: str
    fstype: str = ""
