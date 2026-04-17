"""In-process mock transport.

`MockTransport` implements the `Transport` ABC without a daemon, without
ZMQ, without linuxcnc, and without HAL. It keeps a `StateStore` in
memory, accepts commands that flip bits on it, and fires the dispatch
callbacks synchronously so tests can observe state changes deterministically.

The mock is also used by `qtcnc --mock` at runtime so the GUI can be
brought up without any LinuxCNC machine behind it — a hard requirement
of the framework.

Design notes:

* All mutation happens through `_update(new_state)` which computes the
  diff against the previous state and dispatches it. This guarantees the
  observable behavior matches what a real daemon would do.
* `declare_pins()` is one-shot. After the first call the HAL surface is
  locked (`NackError("hal_locked")` on subsequent calls) to mirror the
  daemon contract: once `hal.component.ready()` has been called, no new
  pins may appear.
* `hello()` fires `_dispatch_connected()` and `close()` fires
  `_dispatch_disconnected("closed")` so GUI components can observe
  connection state transitions even in mock mode.
* Test mutators (`mutate_state`, `mutate_pin`, `inject_error`,
  `inject_lifecycle`) let unit tests simulate the async side of the wire
  directly.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from qtcnc import PROTOCOL_VERSION
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.state import StateStore, apply, diff
from qtcnc.core.types import (
    AxisState,
    CoolantState,
    ErrorMessage,
    ExecState,
    GetToolDbResult,
    InterpSettings,
    InterpState,
    IoState,
    JointState,
    LinearUnits,
    MachineState,
    MotionMode,
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
    ToolDbEntry,
    ToolEntry,
)
from qtcnc.signals import CommandVerb
from qtcnc.transport.base import (
    DeclarePinsResult,
    NackError,
    Transport,
    TransportClosed,
    TransportError,
)


def _default_for(hal_type: HalType) -> Any:
    if hal_type == HalType.BIT:
        return False
    if hal_type == HalType.FLOAT:
        return 0.0
    return 0  # S32, U32


def _plausible_snapshot() -> StateStore:
    """A believable idle-at-estop state used as the default mock snapshot."""
    return StateStore(
        connected=True,
        task_state=TaskState.ESTOP,
        machine=MachineState(
            estop=True,
            powered=False,
            task_mode=TaskMode.MANUAL,
            interp_state=InterpState.IDLE,
            homed=(False, False, False),
            axis_count=3,
            linear_units=LinearUnits.MM,
            axis_mask=0b111,
            joint_count=3,
            spindle_count=1,
            cycle_time=0.01,
            linear_units_per_mm=1.0,
            angular_units_per_deg=1.0,
            kinematics_type=1,
            motion_enabled=False,
            inpos=True,
            coordinates=("X", "Y", "Z"),
        ),
        position=Position(),
        machine_position=Position(),
        dtg=Position(),
        program=ProgramState(program_units=ProgramUnits.MM),
        tool=Tool(),
        tool_in_spindle=0,
        spindles=(SpindleState(index=0),),
        overrides=Overrides(
            feed=1.0,
            rapid=1.0,
            spindles=(1.0,),
            max_velocity=5000.0,
            feed_enabled=True,
            adaptive_enabled=False,
            hold_enabled=True,
        ),
        feed_rate=0.0,
        rapid_rate=0.0,
        active_gcodes=(20, 90, 17, 40, 49, 54, 64, 97),
        active_mcodes=(5, 9),
        task_info=TaskInfo(
            exec_state=ExecState.DONE,
            ini_filename="<mock>",
        ),
        offsets=Offsets(g5x_index=1),
        active_settings=InterpSettings(),
        joints=tuple(JointState() for _ in range(3)),
        axes=tuple(AxisState() for _ in range(3)),
        tool_table=(),
        coolant=CoolantState(),
        probe=ProbeState(),
        io=IoState(
            digital_in=(False,) * 8,
            digital_out=(False,) * 8,
            analog_in=(0.0,) * 8,
            analog_out=(0.0,) * 8,
            misc_error=(0,) * 10,
        ),
        commanded_position=Position(),
        heartbeat=0,
        taskbeat=0,
    )


_NON_MUTATING_VERBS: frozenset[CommandVerb] = frozenset({
    CommandVerb.JOG_CONTINUOUS,
    CommandVerb.JOG_STOP,
    CommandVerb.JOG_INCREMENT,
    CommandVerb.MDI,
    CommandVerb.AUTO_STEP,
    CommandVerb.MIST_ON,
    CommandVerb.MIST_OFF,
    CommandVerb.FLOOD_ON,
    CommandVerb.FLOOD_OFF,
    CommandVerb.DEBUG,
    CommandVerb.MAXVEL,
    CommandVerb.TOOL_OFFSET,
    CommandVerb.LOAD_TOOL_TABLE,
    CommandVerb.TASK_PLAN_SYNCH,
    CommandVerb.OVERRIDE_LIMITS,
    CommandVerb.RESET_INTERPRETER,
    CommandVerb.AUTO_REVERSE,
    CommandVerb.AUTO_FORWARD,
    CommandVerb.SET_MIN_LIMIT,
    CommandVerb.SET_MAX_LIMIT,
    CommandVerb.SET_SPINDLE_OVERRIDE,
    CommandVerb.SET_ADAPTIVE_FEED,
    CommandVerb.SET_ANALOG_OUTPUT,
    CommandVerb.ERROR_MSG,
    CommandVerb.TEXT_MSG,
    CommandVerb.DISPLAY_MSG,
    CommandVerb.SPINDLE_INCREASE,
    CommandVerb.SPINDLE_DECREASE,
    CommandVerb.SPINDLE_CONSTANT,
})


class MockTransport(Transport):
    """Fully synchronous in-process Transport backed by a StateStore."""

    def __init__(self, initial: StateStore | None = None) -> None:
        super().__init__()
        self._state: StateStore = initial if initial is not None else _plausible_snapshot()
        self._pin_specs: dict[str, HalPinSpec] = {}
        self._pin_values: dict[str, Any] = {}
        self._subscribed: set[str] = set()
        self._hal_locked: bool = False
        self._closed: bool = False
        self._hello_called: bool = False
        # Per-instance identity returned in HELLO so tests can simulate
        # a daemon restart by swapping this out and triggering reopen().
        import uuid as _uuid
        self._daemon_instance_id: str = str(_uuid.uuid4())
        # When non-empty, ping() raises TransportError(self._ping_failure)
        # so reconnector tests can simulate a stalled daemon.
        self._ping_failure: str = ""
        # In-memory tool database for mock mode.
        self._tool_db: list[ToolDbEntry] = [
            ToolDbEntry(tool_id=1, pocket=1, z_offset=-25.4, diameter=6.0, comment="6mm endmill"),
            ToolDbEntry(tool_id=2, pocket=2, z_offset=-30.0, diameter=10.0, comment="10mm endmill"),
        ]
        self._random_toolchanger: bool = False
        self._spindle_tool_id: int = 0

    # --- Transport ABC ---

    def hello(self) -> dict[str, Any]:
        self._check_open()
        self._hello_called = True
        self._dispatch_connected()
        return {
            "protocol_version": PROTOCOL_VERSION,
            "daemon": "mock",
            "daemon_instance_id": self._daemon_instance_id,
        }

    def ping(self, nonce: int | None = None) -> dict[str, Any]:
        self._check_open()
        if self._ping_failure:
            raise TransportError(self._ping_failure)
        return {"nonce": nonce} if nonce is not None else {}

    def get_snapshot(self) -> StateStore:
        self._check_open()
        return self._state

    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        self._check_open()
        new_state = self._apply_command(verb, kwargs)
        self._update(new_state)

    def load_program(self, path: str) -> None:
        self._check_open()
        self._dispatch_lifecycle("program_loading", {"path": path})
        new_program = ProgramState(
            path=path,
            total_lines=0,
            current_line=0,
            is_running=False,
            is_paused=False,
        )
        self._update(replace(self._state, program=new_program))
        self._dispatch_lifecycle("program_loaded", {"path": path})

    def declare_pins(self, specs: list[HalPinSpec]) -> DeclarePinsResult:
        self._check_open()
        if self._hal_locked:
            # Subsequent client: inherit if every requested spec matches
            # an existing declaration exactly (name + type + dir).
            for spec in specs:
                existing = self._pin_specs.get(spec.name)
                if existing is None:
                    raise NackError("hal_locked")
                if existing.type != spec.type or existing.dir != spec.dir:
                    raise NackError(f"pin_type_mismatch:{spec.name}")
            return DeclarePinsResult(
                created=[s.name for s in specs],
                inherited=True,
            )
        created: list[str] = []
        seen: set[str] = set()
        for spec in specs:
            if spec.name in seen or spec.name in self._pin_specs:
                raise NackError(f"duplicate pin: {spec.name}")
            seen.add(spec.name)
        for spec in specs:
            self._pin_specs[spec.name] = spec
            self._pin_values[spec.name] = (
                spec.initial if spec.initial is not None else _default_for(spec.type)
            )
            created.append(spec.name)
        self._hal_locked = True
        return DeclarePinsResult(created=created, inherited=False)

    def write_pin(self, name: str, value: Any) -> None:
        self._check_open()
        if name not in self._pin_specs:
            raise NackError(f"unknown pin: {name}")
        spec = self._pin_specs[name]
        if spec.dir == HalDir.IN:
            raise NackError(f"cannot write IN pin: {name}")
        self._pin_values[name] = value
        self._dispatch_hal_pin_update(name, value)

    def subscribe_pin(self, name: str) -> None:
        self._check_open()
        self._subscribed.add(name)

    def get_tool_db(self) -> GetToolDbResult:
        self._check_open()
        return GetToolDbResult(
            tools=tuple(self._tool_db),
            spindle_tool_id=self._spindle_tool_id,
            random_toolchanger=self._random_toolchanger,
        )

    def add_tool(self, tool_id: int, pocket: int, **fields: Any) -> None:
        self._check_open()
        for entry in self._tool_db:
            if entry.tool_id == tool_id:
                raise NackError(f"tool {tool_id} already exists")
        self._tool_db.append(ToolDbEntry(tool_id=tool_id, pocket=pocket, **fields))
        self._tool_db.sort(key=lambda e: e.tool_id)

    def remove_tool(self, tool_id: int) -> None:
        self._check_open()
        self._tool_db = [e for e in self._tool_db if e.tool_id != tool_id]

    def update_tool(self, tool_id: int, **fields: Any) -> None:
        self._check_open()
        for i, entry in enumerate(self._tool_db):
            if entry.tool_id == tool_id:
                update = {k: v for k, v in fields.items()
                          if hasattr(entry, k) and k != "tool_id"}
                from dataclasses import replace as _replace
                self._tool_db[i] = _replace(entry, **update)
                return
        raise NackError(f"tool {tool_id} not found")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._dispatch_disconnected("closed")

    @property
    def is_connected(self) -> bool:
        return not self._closed and self._hello_called

    # --- command dispatch ---

    def _apply_command(self, verb: CommandVerb, kwargs: dict[str, Any]) -> StateStore:
        s = self._state
        if verb == CommandVerb.STATE_ESTOP:
            return replace(
                s,
                machine=replace(s.machine, estop=True, powered=False),
                task_state=TaskState.ESTOP,
            )
        if verb == CommandVerb.STATE_ESTOP_RESET:
            return replace(
                s,
                machine=replace(s.machine, estop=False),
                task_state=TaskState.ESTOP_RESET,
            )
        if verb == CommandVerb.STATE_ON:
            if s.machine.estop:
                raise NackError("cannot power on while estopped")
            return replace(
                s,
                machine=replace(s.machine, powered=True),
                task_state=TaskState.ON,
            )
        if verb == CommandVerb.STATE_OFF:
            return replace(
                s,
                machine=replace(s.machine, powered=False),
                task_state=TaskState.OFF,
            )
        if verb == CommandVerb.SET_MODE:
            mode = kwargs.get("mode")
            if not isinstance(mode, TaskMode):
                raise NackError(f"set_mode requires mode=TaskMode, got {mode!r}")
            return replace(s, machine=replace(s.machine, task_mode=mode))
        if verb == CommandVerb.HOME:
            joint = int(kwargs.get("joint", -1))
            if joint < 0:
                return replace(
                    s,
                    machine=replace(
                        s.machine,
                        homed=tuple(True for _ in range(s.machine.axis_count)),
                    ),
                )
            return replace(
                s,
                machine=replace(
                    s.machine, homed=_set_homed(s.machine.homed, joint, True),
                ),
            )
        if verb == CommandVerb.UNHOME:
            joint = int(kwargs.get("joint", -1))
            if joint < 0:
                return replace(
                    s,
                    machine=replace(
                        s.machine,
                        homed=tuple(False for _ in range(s.machine.axis_count)),
                    ),
                )
            return replace(
                s,
                machine=replace(
                    s.machine, homed=_set_homed(s.machine.homed, joint, False),
                ),
            )
        if verb == CommandVerb.FEEDRATE:
            return replace(
                s, overrides=replace(s.overrides, feed=float(kwargs["value"])),
            )
        if verb == CommandVerb.RAPIDRATE:
            return replace(
                s, overrides=replace(s.overrides, rapid=float(kwargs["value"])),
            )
        if verb == CommandVerb.SPINDLEOVERRIDE:
            idx = int(kwargs.get("index", 0))
            value = float(kwargs["value"])
            new_sp = list(s.overrides.spindles)
            while len(new_sp) <= idx:
                new_sp.append(1.0)
            new_sp[idx] = value
            return replace(s, overrides=replace(s.overrides, spindles=tuple(new_sp)))
        if verb in (CommandVerb.AUTO_RUN, CommandVerb.AUTO_RESUME):
            return replace(s, program=replace(s.program, is_running=True, is_paused=False))
        if verb == CommandVerb.AUTO_PAUSE:
            return replace(s, program=replace(s.program, is_paused=True))
        if verb == CommandVerb.ABORT:
            return replace(s, program=replace(s.program, is_running=False, is_paused=False))
        if verb == CommandVerb.SPINDLE_FORWARD:
            idx = int(kwargs.get("index", 0))
            return replace(s, spindles=_set_spindle(
                s.spindles, idx, direction=SpindleDir.FORWARD, enabled=True,
                speed=float(kwargs.get("speed", 0.0)),
            ))
        if verb == CommandVerb.SPINDLE_REVERSE:
            idx = int(kwargs.get("index", 0))
            return replace(s, spindles=_set_spindle(
                s.spindles, idx, direction=SpindleDir.REVERSE, enabled=True,
                speed=float(kwargs.get("speed", 0.0)),
            ))
        if verb == CommandVerb.SPINDLE_OFF:
            idx = int(kwargs.get("index", 0))
            return replace(s, spindles=_set_spindle(
                s.spindles, idx, direction=SpindleDir.STOP, enabled=False, speed=0.0,
            ))
        if verb == CommandVerb.TRAJ_MODE:
            mode = kwargs.get("mode")
            if not isinstance(mode, MotionMode):
                raise NackError(f"traj_mode requires MotionMode, got {mode!r}")
            return replace(s, machine=replace(s.machine, motion_mode=mode))
        if verb == CommandVerb.BRAKE_ENGAGE:
            idx = int(kwargs.get("index", 0))
            return replace(s, spindles=_set_spindle_brake(s.spindles, idx, True))
        if verb == CommandVerb.BRAKE_RELEASE:
            idx = int(kwargs.get("index", 0))
            return replace(s, spindles=_set_spindle_brake(s.spindles, idx, False))
        if verb == CommandVerb.SET_FEED_OVERRIDE:
            return replace(
                s, overrides=replace(
                    s.overrides, feed_enabled=bool(kwargs.get("enabled", True)),
                ),
            )
        if verb == CommandVerb.SET_FEED_HOLD:
            return replace(
                s, overrides=replace(
                    s.overrides, hold_enabled=bool(kwargs.get("enabled", True)),
                ),
            )
        if verb == CommandVerb.SET_OPTIONAL_STOP:
            return replace(
                s, task_info=replace(
                    s.task_info, optional_stop=bool(kwargs.get("enabled", False)),
                ),
            )
        if verb == CommandVerb.SET_BLOCK_DELETE:
            return replace(
                s, task_info=replace(
                    s.task_info, block_delete=bool(kwargs.get("enabled", False)),
                ),
            )
        if verb == CommandVerb.SET_DIGITAL_OUTPUT:
            idx = int(kwargs.get("index", 0))
            value = bool(kwargs.get("value", False))
            new_out = list(s.io.digital_out)
            while len(new_out) <= idx:
                new_out.append(False)
            new_out[idx] = value
            return replace(s, io=replace(s.io, digital_out=tuple(new_out)))
        if verb == CommandVerb.SET_PROGRAM_TOOLS:
            raw = kwargs.get("tools", ())
            tools = frozenset(int(t) for t in raw if int(t) > 0)
            return replace(s, program=replace(s.program, requested_tools=tools))
        if verb in _NON_MUTATING_VERBS:
            return s
        raise NackError(f"unsupported verb: {verb}")

    # --- state update plumbing ---

    def _update(self, new_state: StateStore) -> None:
        changes = diff(self._state, new_state)
        self._state = new_state
        if changes:
            self._dispatch_state_diff(changes)

    def _check_open(self) -> None:
        if self._closed:
            raise TransportClosed("mock transport is closed")

    # --- test-facing mutators ---

    def mutate_state(self, **field_updates: Any) -> None:
        """Update the StateStore directly and fire a state_diff callback.

        Keyword args are StateStore field names, values are new values.
        """
        new_state = replace(self._state, **field_updates)
        self._update(new_state)

    def mutate_state_from_changes(self, changes: list[tuple[str, Any]]) -> None:
        """Apply a list of (field_name, value) changes and fire a diff."""
        new_state = apply(self._state, changes)
        self._update(new_state)

    def mutate_pin(self, name: str, value: Any) -> None:
        """Simulate a HAL pin update arriving from the daemon."""
        self._pin_values[name] = value
        self._dispatch_hal_pin_update(name, value)

    def inject_error(self, err: ErrorMessage) -> None:
        self._dispatch_error(err)

    def inject_lifecycle(self, tag: str, payload: dict[str, Any] | None = None) -> None:
        self._dispatch_lifecycle(tag, payload or {})

    def pin_value(self, name: str) -> Any:
        """Peek at a pin's current value. For tests and debugging."""
        return self._pin_values.get(name)

    def simulate_daemon_restart(self) -> None:
        """Roll the instance id and clear the hello flag so the next
        hello() looks like a brand-new daemon to a Reconnector. Tests
        use this to verify daemon_restarted detection."""
        import uuid as _uuid
        self._daemon_instance_id = str(_uuid.uuid4())
        self._hello_called = False

    def simulate_ping_failure(self, reason: str) -> None:
        """Make the next ping() raise TransportError(reason). Pass an
        empty string to clear the failure."""
        self._ping_failure = reason


def _set_homed(homed: tuple[bool, ...], axis: int, value: bool) -> tuple[bool, ...]:
    if axis < 0:
        return homed
    new = list(homed)
    while len(new) <= axis:
        new.append(False)
    new[axis] = value
    return tuple(new)


def _set_spindle(
    spindles: tuple[SpindleState, ...],
    idx: int,
    *,
    direction: SpindleDir,
    enabled: bool,
    speed: float,
) -> tuple[SpindleState, ...]:
    new = list(spindles)
    while len(new) <= idx:
        new.append(SpindleState(index=len(new)))
    new[idx] = replace(
        new[idx], direction=direction, enabled=enabled, speed=speed,
    )
    return tuple(new)


def _set_spindle_brake(
    spindles: tuple[SpindleState, ...], idx: int, engaged: bool,
) -> tuple[SpindleState, ...]:
    new = list(spindles)
    while len(new) <= idx:
        new.append(SpindleState(index=len(new)))
    new[idx] = replace(new[idx], brake=engaged)
    return tuple(new)
