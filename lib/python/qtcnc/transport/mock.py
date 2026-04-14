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
    ErrorMessage,
    InterpState,
    MachineState,
    Overrides,
    Position,
    ProgramState,
    SpindleDir,
    SpindleState,
    TaskMode,
    TaskState,
    Tool,
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
        ),
        position=Position(),
        machine_position=Position(),
        dtg=Position(),
        program=ProgramState(),
        tool=Tool(),
        tool_in_spindle=0,
        spindles=(SpindleState(index=0),),
        overrides=Overrides(feed=1.0, rapid=1.0, spindles=(1.0,)),
        feed_rate=0.0,
        rapid_rate=0.0,
        active_gcodes=(20, 90, 17, 40, 49, 54, 64, 97),
        active_mcodes=(5, 9),
    )


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
        if verb == CommandVerb.ESTOP:
            return replace(
                s,
                machine=replace(s.machine, estop=True, powered=False),
                task_state=TaskState.ESTOP,
            )
        if verb == CommandVerb.ESTOP_RESET:
            return replace(
                s,
                machine=replace(s.machine, estop=False),
                task_state=TaskState.ESTOP_RESET,
            )
        if verb == CommandVerb.POWER_ON:
            if s.machine.estop:
                raise NackError("cannot power on while estopped")
            return replace(
                s,
                machine=replace(s.machine, powered=True),
                task_state=TaskState.ON,
            )
        if verb == CommandVerb.POWER_OFF:
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
        if verb == CommandVerb.HOME_AXIS:
            axis = int(kwargs.get("axis", -1))
            return replace(s, machine=replace(s.machine, homed=_set_homed(s.machine.homed, axis, True)))
        if verb == CommandVerb.UNHOME_AXIS:
            axis = int(kwargs.get("axis", -1))
            return replace(s, machine=replace(s.machine, homed=_set_homed(s.machine.homed, axis, False)))
        if verb == CommandVerb.HOME_ALL:
            return replace(
                s,
                machine=replace(
                    s.machine,
                    homed=tuple(True for _ in range(s.machine.axis_count)),
                ),
            )
        if verb == CommandVerb.SET_FEED_OVERRIDE:
            return replace(s, overrides=replace(s.overrides, feed=float(kwargs["value"])))
        if verb == CommandVerb.SET_RAPID_OVERRIDE:
            return replace(s, overrides=replace(s.overrides, rapid=float(kwargs["value"])))
        if verb == CommandVerb.SET_SPINDLE_OVERRIDE:
            idx = int(kwargs.get("index", 0))
            value = float(kwargs["value"])
            new_sp = list(s.overrides.spindles)
            while len(new_sp) <= idx:
                new_sp.append(1.0)
            new_sp[idx] = value
            return replace(s, overrides=replace(s.overrides, spindles=tuple(new_sp)))
        if verb in (CommandVerb.PROGRAM_RUN, CommandVerb.PROGRAM_RESUME):
            return replace(s, program=replace(s.program, is_running=True, is_paused=False))
        if verb == CommandVerb.PROGRAM_PAUSE:
            return replace(s, program=replace(s.program, is_paused=True))
        if verb == CommandVerb.PROGRAM_STOP:
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
        if verb == CommandVerb.SPINDLE_STOP:
            idx = int(kwargs.get("index", 0))
            return replace(s, spindles=_set_spindle(
                s.spindles, idx, direction=SpindleDir.STOP, enabled=False, speed=0.0,
            ))
        if verb in (
            CommandVerb.JOG_START, CommandVerb.JOG_STOP, CommandVerb.JOG_INCREMENT,
            CommandVerb.MDI, CommandVerb.PROGRAM_STEP,
            CommandVerb.MIST_ON, CommandVerb.MIST_OFF,
            CommandVerb.FLOOD_ON, CommandVerb.FLOOD_OFF,
        ):
            # Accepted but no state mutation in the mock.
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
