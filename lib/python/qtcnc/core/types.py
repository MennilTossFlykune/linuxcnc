"""Value types for the qtcnc framework.

All types are frozen+slots dataclasses. Signals emit new instances rather
than mutating in place — identity equals value. Stdlib only; safe to import
from Qt Designer plugin loaders.

State predicates
----------------
MachineState and ProgramState expose boolean @property helpers that
consolidate the "is the machine in a state where X is allowed?" checks
callers previously inlined. Composite predicates on StateStore itself
live in `qtcnc.core.state`.

MachineState primitives:
    is_ready, is_all_homed, is_idle, is_manual, is_auto, is_mdi

MachineState composites:
    can_jog, can_home, can_unhome, can_spindle, can_use_teleop_jog

MachineState axis/joint mapping:
    axis_letters — Cartesian letters the machine exposes, derived from
    axis_mask. Gantry configs (e.g. XYYZ with 4 joints, 3 axes) return
    ("X", "Y", "Z"); `axis_count` stays a joint-level quantity.

    coordinates — per-joint axis letter tuple from [TRAJ]COORDINATES.
    On a gantry with COORDINATES = X Y Y Z this is ("X", "Y", "Y", "Z"):
    joint 0 → X, joint 1 → first Y, joint 2 → second Y, joint 3 → Z.

    joint_for_axis(axis_index) — first joint mapped to an axis index
    axis_for_joint(joint) — axis index a joint is mapped to
    joints_for_axis(axis_index) — all joints for an axis (gantry: multiple)

ProgramState:
    has_program, is_executing, is_editable, is_runnable

@property is safe on frozen slotted dataclasses: `fields()`, `diff()`,
`apply()`, and the wire codec all iterate over declared fields only
and never touch properties.

Sub-dataclass inventory
-----------------------
Direct-motion state lives on `MachineState`. Program-text state lives on
`ProgramState`. Every other slice of `linuxcnc.stat()` is grouped into a
focused sub-dataclass so `StateStore`'s top-level footprint stays compact
while covering every stat field:

    TaskInfo            task daemon bookkeeping (exec_state, rcs_state,
                        echo_serial_number, optional_stop, block_delete,
                        ini_filename, active_command_line, ...)
    Offsets             g5x/g92/tool offsets and XY rotation
    InterpSettings      interpreter 5-tuple (seq, feed, speed, tolerances)
    JointState          per-joint snapshot (22 fields)
    AxisState           per-axis Cartesian snapshot (velocity + limits)
    ToolEntry           one row of the tool table
    CoolantState        mist + flood
    ProbeState          probe_tripped, probing, probe_val, probed_position
    IoState             digital/analog IO + aux estop + tool pocket state
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


class MotionMode(IntEnum):
    """Trajectory-planner coordinate mode.

    Integer values match linuxcnc.TRAJ_MODE_FREE/COORD/TELEOP so the
    daemon can cast `stat.motion_mode` directly without importing the
    linuxcnc module on the client side.
    """

    FREE = 1
    COORD = 2
    TELEOP = 3


class LinearUnits(IntEnum):
    """Machine linear-unit system as reported by `[TRAJ]LINEAR_UNITS`.

    The daemon reads `stat.linear_units` (a float in "units per mm" per
    `src/emc/nml_intf/emc_nml.hh`) and maps 1.0 → MM, 1/25.4 → INCH. The
    enum keeps the client side unit-aware without importing linuxcnc.
    """

    MM = 1
    INCH = 2


class ExecState(IntEnum):
    """Task `exec_state` — wait reason for the task main loop.

    Integer values mirror `EMC_TASK_EXEC::*` in
    `src/emc/nml_intf/emc.hh`.
    """

    ERROR = 1
    DONE = 2
    WAITING_FOR_MOTION = 3
    WAITING_FOR_MOTION_QUEUE = 4
    WAITING_FOR_IO = 5
    WAITING_FOR_MOTION_AND_IO = 7
    WAITING_FOR_DELAY = 8
    WAITING_FOR_SYSTEM_CMD = 9
    WAITING_FOR_SPINDLE_ORIENTED = 10


class ProgramUnits(IntEnum):
    """Loaded program's length-unit mode — `stat.program_units`.

    Integer values mirror `CANON_UNITS` in `src/emc/nml_intf/canon.hh`.
    """

    INCH = 1
    MM = 2
    CM = 3


_ALL_AXIS_LETTERS: tuple[str, ...] = ("X", "Y", "Z", "A", "B", "C", "U", "V", "W")


def _axis_letters_from_mask(mask: int) -> tuple[str, ...]:
    return tuple(letter for i, letter in enumerate(_ALL_AXIS_LETTERS) if mask & (1 << i))


class SpindleDir(IntEnum):
    REVERSE = -1
    STOP = 0
    FORWARD = 1


class ErrorSeverity(IntEnum):
    INFO = 0
    OPERATOR_DISPLAY = 1
    OPERATOR_ERROR = 2
    NML_ERROR = 3


class MessageSeverity(IntEnum):
    DEBUG = 0
    INFO = 1
    WARNING = 2
    ERROR = 3


class MessageSource(IntEnum):
    LINUXCNC = 0
    FRAMEWORK = 1
    USER = 2


@dataclass(frozen=True, slots=True)
class Message:
    severity: MessageSeverity
    source: MessageSource
    text: str
    timestamp: float


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
    motion_mode: MotionMode = MotionMode.FREE
    kinematics_identity: bool = False
    linear_units: LinearUnits = LinearUnits.MM
    axis_mask: int = 0b111  # XYZ — smallest sensible default
    # --- trajectory/topology counts (from stat.joints, stat.spindles, …) ---
    joint_count: int = 0
    spindle_count: int = 0
    num_extrajoints: int = 0
    # --- trajectory kinematics / units ---
    cycle_time: float = 0.0
    linear_units_per_mm: float = 1.0
    angular_units_per_deg: float = 1.0
    kinematics_type: int = 1
    # --- motion / trajectory runtime ---
    motion_enabled: bool = False
    inpos: bool = False
    queue: int = 0
    active_queue: int = 0
    queue_full: bool = False
    motion_id: int = 0
    single_stepping: bool = False
    commanded_velocity: float = 0.0
    commanded_acceleration: float = 0.0
    max_acceleration: float = 0.0
    distance_to_go_scalar: float = 0.0
    # --- per-joint axis letter mapping from [TRAJ]COORDINATES ---
    coordinates: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        """Out of estop and energized. Precondition for every motion command."""
        return (not self.estop) and self.powered

    @property
    def is_all_homed(self) -> bool:
        """Every joint the controller exposes is homed. A machine with
        zero joints is treated as not-homed so jog/home gates stay safe.
        """
        return bool(self.homed) and all(self.homed)

    @property
    def is_idle(self) -> bool:
        """The interpreter is at rest. Motion-settled (inpos) is a
        separate concern tracked by the daemon's stat poll.
        """
        return self.interp_state == InterpState.IDLE

    @property
    def is_manual(self) -> bool:
        return self.task_mode == TaskMode.MANUAL

    @property
    def is_auto(self) -> bool:
        return self.task_mode == TaskMode.AUTO

    @property
    def is_mdi(self) -> bool:
        return self.task_mode == TaskMode.MDI

    @property
    def can_jog(self) -> bool:
        """Jogging is allowed when the machine is ready, in MANUAL, and
        the interpreter is idle so the jog cannot collide with an
        in-progress program or MDI read.
        """
        return self.is_ready and self.is_manual and self.is_idle

    @property
    def can_home(self) -> bool:
        """Homing is allowed any time the machine is ready and in
        MANUAL. interp_state is intentionally not checked: homing
        pre-empts, and it is what moves the interpreter away from a
        blocked idle.
        """
        return self.is_ready and self.is_manual

    @property
    def can_unhome(self) -> bool:
        return self.is_ready and self.is_manual

    @property
    def can_spindle(self) -> bool:
        """Standalone spindle start/stop is gated on machine readiness;
        task_mode is not checked because the spindle is also commandable
        from MDI and AUTO with the usual interpreter interlocks.
        """
        return self.is_ready

    @property
    def can_use_teleop_jog(self) -> bool:
        """Axis-coordinate (teleop) jogging requires identity kinematics
        and a fully homed machine so the kinematics map can translate
        axis deltas into joint deltas.
        """
        return self.kinematics_identity and self.is_all_homed

    @property
    def axis_letters(self) -> tuple[str, ...]:
        """Cartesian axis letters the machine exposes, ordered X..W.

        Derived from `axis_mask`. For a gantry config declared as
        `COORDINATES = X Y Y Z` the bitmask still reports three unique
        axes (X, Y, Z) while `len(homed)` reports four joints.
        """
        return _axis_letters_from_mask(self.axis_mask)

    @property
    def num_axes(self) -> int:
        """Count of Cartesian axes exposed via `axis_mask`."""
        return bin(self.axis_mask & 0x1FF).count("1")

    def axis_for_joint(self, joint: int) -> int:
        """Axis index (0–8) the given joint is mapped to.

        Uses the `coordinates` tuple when available. Falls back to
        identity (joint == axis index) when no mapping is present.
        """
        if self.coordinates and 0 <= joint < len(self.coordinates):
            letter = self.coordinates[joint].upper()
            try:
                return _ALL_AXIS_LETTERS.index(letter)
            except ValueError:
                pass
        return joint

    def joint_for_axis(self, axis_index: int) -> int:
        """First joint number mapped to the given axis index.

        On a gantry with COORDINATES = X Y Y Z, axis_index=1 (Y) returns
        joint 1 (the first Y). Falls back to identity when no mapping
        is present.
        """
        if self.coordinates and 0 <= axis_index < len(_ALL_AXIS_LETTERS):
            target = _ALL_AXIS_LETTERS[axis_index]
            for j, letter in enumerate(self.coordinates):
                if letter.upper() == target:
                    return j
        return axis_index

    def joints_for_axis(self, axis_index: int) -> tuple[int, ...]:
        """All joint numbers mapped to the given axis index.

        On a gantry with COORDINATES = X Y Y Z, axis_index=1 (Y) returns
        (1, 2). Returns a single-element tuple when no mapping is present.
        """
        if self.coordinates and 0 <= axis_index < len(_ALL_AXIS_LETTERS):
            target = _ALL_AXIS_LETTERS[axis_index]
            joints = tuple(
                j for j, letter in enumerate(self.coordinates)
                if letter.upper() == target
            )
            if joints:
                return joints
        return (axis_index,)


@dataclass(frozen=True, slots=True)
class ProgramState:
    path: str = ""
    total_lines: int = 0
    current_line: int = 0
    is_running: bool = False
    is_paused: bool = False
    motion_line: int = 0
    program_units: ProgramUnits = ProgramUnits.MM
    requested_tools: frozenset[int] = field(default_factory=frozenset)

    @property
    def has_program(self) -> bool:
        """A non-empty path is loaded. Existence on disk is verified by
        the load path before PROGRAM_LOADED fires.
        """
        return bool(self.path)

    @property
    def is_executing(self) -> bool:
        """A program is actively running or paused mid-run."""
        return self.is_running or self.is_paused

    @property
    def is_editable(self) -> bool:
        """A program may be edited or closed only when it is not mid-execution."""
        return not self.is_executing

    @property
    def is_runnable(self) -> bool:
        """A program can be started only when a file is loaded and
        not already running. Machine-side readiness is layered on top
        in StateStore.can_run_program.
        """
        return self.has_program and not self.is_executing


@dataclass(frozen=True, slots=True)
class Tool:
    id: int = 0
    pocket: int = 0
    offset: Position = field(default_factory=Position)
    diameter: float = 0.0
    comment: str = ""
    frontangle: float = 0.0
    backangle: float = 0.0
    orientation: int = 0


@dataclass(frozen=True, slots=True)
class SpindleState:
    index: int = 0
    speed: float = 0.0
    direction: SpindleDir = SpindleDir.STOP
    enabled: bool = False
    brake: bool = False
    override_enabled: bool = True
    homed: bool = False
    orient_state: int = 0
    orient_fault: int = 0


@dataclass(frozen=True, slots=True)
class Overrides:
    feed: float = 1.0
    rapid: float = 1.0
    spindles: tuple[float, ...] = (1.0,)
    max_velocity: float = 0.0
    feed_enabled: bool = True
    adaptive_enabled: bool = False
    hold_enabled: bool = True


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


@dataclass(frozen=True, slots=True)
class TaskInfo:
    """Task daemon bookkeeping — `stat.state/exec_state/...`.

    Grouped here so `MachineState` stays focused on motion/power and
    `ProgramState` stays focused on the g-code file. `active_command_line`
    holds the text of the MDI or motion command the task is currently
    executing (from `stat.command`).
    """

    rcs_state: int = 0
    echo_serial_number: int = 0
    exec_state: ExecState = ExecState.DONE
    call_level: int = 0
    current_line: int = 0
    active_command_line: str = ""
    interpreter_errcode: int = 0
    optional_stop: bool = False
    block_delete: bool = False
    task_paused: int = 0
    input_timeout: bool = False
    ini_filename: str = ""
    delay_left: float = 0.0
    queued_mdi_commands: int = 0
    debug_mask: int = 0


@dataclass(frozen=True, slots=True)
class Offsets:
    """Coordinate-system offsets active in the interpreter."""

    g5x_index: int = 1
    g5x: Position = field(default_factory=Position)
    g92: Position = field(default_factory=Position)
    tool_offset: Position = field(default_factory=Position)
    rotation_xy: float = 0.0


@dataclass(frozen=True, slots=True)
class InterpSettings:
    """`stat.settings` — the 5-element interpreter settings array:
    sequence number, feed, speed, G64 blend tolerance, naive CAM tolerance.
    """

    sequence_number: float = 0.0
    feed: float = 0.0
    speed: float = 0.0
    g64_blend_tolerance: float = 0.0
    naive_cam_tolerance: float = 0.0


@dataclass(frozen=True, slots=True)
class JointState:
    """Per-joint snapshot — one entry per hardware joint.

    `stat.joint` always returns `EMCMOT_MAX_JOINTS` entries; qtcnc trims
    to `machine.joint_count` so the wire and widgets see only the
    joints the controller actually exposes.
    """

    joint_type: int = 0
    units: float = 1.0
    backlash: float = 0.0
    min_position_limit: float = 0.0
    max_position_limit: float = 0.0
    max_ferror: float = 0.0
    min_ferror: float = 0.0
    ferror_current: float = 0.0
    ferror_highmark: float = 0.0
    output: float = 0.0
    input: float = 0.0
    velocity: float = 0.0
    inpos: bool = False
    homing_state: int = 0
    homed: bool = False
    fault: bool = False
    enabled: bool = False
    min_soft_limit: bool = False
    max_soft_limit: bool = False
    min_hard_limit: bool = False
    max_hard_limit: bool = False
    override_limits: bool = False

    @property
    def is_homing(self) -> bool:
        """True while this joint's homing sequence is active."""
        return bool(self.homing_state)


@dataclass(frozen=True, slots=True)
class AxisState:
    """Per-axis (Cartesian letter) snapshot. `stat.axis` always returns
    nine entries; qtcnc trims to the axes set in `machine.axis_mask`.
    """

    velocity: float = 0.0
    min_position_limit: float = 0.0
    max_position_limit: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """One row of `stat.tool_table` — mirrors the 14-field tool struct
    that the linuxcnc C extension exposes per pocket.
    """

    id: int = 0
    offset: Position = field(default_factory=Position)
    diameter: float = 0.0
    frontangle: float = 0.0
    backangle: float = 0.0
    orientation: int = 0


@dataclass(frozen=True, slots=True)
class CoolantState:
    mist: bool = False
    flood: bool = False


@dataclass(frozen=True, slots=True)
class ProbeState:
    tripped: bool = False
    probing: bool = False
    value: int = 0
    probed_position: Position = field(default_factory=Position)


@dataclass(frozen=True, slots=True)
class IoState:
    """Discrete and analog I/O snapshots, plus tool-pocket and aux-estop
    state from the I/O subsystem.

    `aux_estop` is the hardware estop input surfaced by `io.aux.estop`;
    `MachineState.estop` reflects the task-level `task_state == ESTOP`
    view. Both are preserved under distinct names.
    """

    digital_in: tuple[bool, ...] = ()
    digital_out: tuple[bool, ...] = ()
    analog_in: tuple[float, ...] = ()
    analog_out: tuple[float, ...] = ()
    misc_error: tuple[int, ...] = ()
    pocket_prepped: int = -1
    tool_from_pocket: int = 0
    aux_estop: bool = False


@dataclass(frozen=True, slots=True)
class ToolDbEntry:
    """One row of the qtcnc tool database — sqlite-backed, with all
    nine axis offsets as flat floats and a comment field.

    Distinct from `ToolEntry` (which mirrors `stat.tool_table`'s
    compact 14-field struct). `ToolDbEntry` carries the full editable
    surface the tool-offset widget presents to the operator.
    """

    tool_id: int = 0
    pocket: int = 0
    x_offset: float = 0.0
    y_offset: float = 0.0
    z_offset: float = 0.0
    a_offset: float = 0.0
    b_offset: float = 0.0
    c_offset: float = 0.0
    u_offset: float = 0.0
    v_offset: float = 0.0
    w_offset: float = 0.0
    diameter: float = 0.0
    frontangle: float = 0.0
    backangle: float = 0.0
    orientation: int = 0
    comment: str = ""


@dataclass(frozen=True, slots=True)
class GetToolDbResult:
    """Response payload for a GET_TOOL_DB request."""

    tools: tuple[ToolDbEntry, ...] = ()
    spindle_tool_id: int = 0
    random_toolchanger: bool = False
