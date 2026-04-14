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
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Optional

from qtcnc import PROTOCOL_VERSION
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.state import StateStore, diff
from qtcnc.core.types import (
    ErrorMessage,
    ErrorSeverity,
    InterpState,
    MachineState,
    MotionType,
    Overrides,
    Position,
    ProgramState,
    SpindleDir,
    SpindleState,
    TaskMode,
    TaskState,
    Tool,
)
from qtcnc.signals import CommandVerb, Lifecycle
from qtcnc.transport.base import DeclarePinsResult, NackError
from qtcnc.transport.zmq_server import ZmqServerTransport


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


def stat_to_state_store(stat: Any, lc: Any) -> StateStore:
    """Build a StateStore snapshot from a polled `linuxcnc.stat` object.

    This function is the single source of truth for the mapping. It is
    exported so tests can exercise it directly with a fake stat.
    """
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

    homed_raw = getattr(stat, "homed", ()) or ()
    homed = tuple(bool(h) for h in homed_raw)
    axis_count = len(homed) or 3

    machine = MachineState(
        estop=estop,
        powered=powered,
        task_mode=task_mode,
        interp_state=interp_state,
        motion_type=motion_type,
        homed=homed,
        axis_count=axis_count,
    )

    position = _position_from_tuple(getattr(stat, "actual_position", ()))
    machine_position = _position_from_tuple(
        getattr(stat, "joint_actual_position", ()) or getattr(stat, "position", ())
    )
    dtg = _position_from_tuple(getattr(stat, "dtg", ()))

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
    program = ProgramState(
        path=str(getattr(stat, "file", "") or ""),
        total_lines=0,
        current_line=current_line,
        is_running=is_running,
        is_paused=is_paused,
    )

    tool_in_spindle = int(getattr(stat, "tool_in_spindle", 0) or 0)
    tool = Tool(id=tool_in_spindle)

    raw_spindles = getattr(stat, "spindle", ()) or ()
    spindles: list[SpindleState] = []
    spindle_overrides: list[float] = []
    for i, sp in enumerate(raw_spindles):
        def _get(key: str, default: Any) -> Any:
            if isinstance(sp, dict):
                return sp.get(key, default)
            return getattr(sp, key, default)

        try:
            direction = SpindleDir(int(_get("direction", 0)))
        except ValueError:
            direction = SpindleDir.STOP
        spindles.append(SpindleState(
            index=i,
            speed=float(_get("speed", 0.0) or 0.0),
            direction=direction,
            enabled=bool(_get("enabled", False)),
        ))
        spindle_overrides.append(float(_get("override", 1.0) or 1.0))
    if not spindles:
        spindles = [SpindleState()]
        spindle_overrides = [1.0]

    overrides = Overrides(
        feed=float(getattr(stat, "feedrate", 1.0) or 1.0),
        rapid=float(getattr(stat, "rapidrate", 1.0) or 1.0),
        spindles=tuple(spindle_overrides),
    )

    active_gcodes = tuple(
        int(g) for g in (getattr(stat, "gcodes", ()) or ()) if g is not None and g != -1
    )
    active_mcodes = tuple(
        int(m) for m in (getattr(stat, "mcodes", ()) or ()) if m is not None and m != -1
    )

    return StateStore(
        connected=True,
        task_state=task_state,
        machine=machine,
        position=position,
        machine_position=machine_position,
        dtg=dtg,
        program=program,
        tool=tool,
        tool_in_spindle=tool_in_spindle,
        spindles=tuple(spindles),
        overrides=overrides,
        feed_rate=float(getattr(stat, "current_vel", 0.0) or 0.0),
        rapid_rate=float(getattr(stat, "max_velocity", 0.0) or 0.0),
        active_gcodes=active_gcodes,
        active_mcodes=active_mcodes,
    )


# ---------------------------------------------------------------------------
# CommandVerb -> linuxcnc.command() dispatch
# ---------------------------------------------------------------------------


def execute_command(cmd: Any, lc: Any, verb: CommandVerb, kwargs: dict[str, Any]) -> None:
    """Translate a `CommandVerb` into the equivalent `linuxcnc.command()` call."""
    if verb == CommandVerb.ESTOP:
        cmd.state(lc.STATE_ESTOP)
    elif verb == CommandVerb.ESTOP_RESET:
        cmd.state(lc.STATE_ESTOP_RESET)
    elif verb == CommandVerb.POWER_ON:
        cmd.state(lc.STATE_ON)
    elif verb == CommandVerb.POWER_OFF:
        cmd.state(lc.STATE_OFF)
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
    elif verb == CommandVerb.HOME_AXIS:
        cmd.home(int(kwargs.get("axis", -1)))
    elif verb == CommandVerb.UNHOME_AXIS:
        cmd.unhome(int(kwargs.get("axis", -1)))
    elif verb == CommandVerb.HOME_ALL:
        cmd.home(-1)
    elif verb == CommandVerb.JOG_START:
        cmd.jog(
            lc.JOG_CONTINUOUS,
            int(kwargs.get("joint", kwargs.get("axis", 0))),
            float(kwargs.get("speed", 0.0)),
        )
    elif verb == CommandVerb.JOG_STOP:
        cmd.jog(lc.JOG_STOP, int(kwargs.get("joint", kwargs.get("axis", 0))))
    elif verb == CommandVerb.JOG_INCREMENT:
        cmd.jog(
            lc.JOG_INCREMENT,
            int(kwargs.get("joint", kwargs.get("axis", 0))),
            float(kwargs.get("speed", 0.0)),
            float(kwargs.get("distance", 0.0)),
        )
    elif verb == CommandVerb.SET_FEED_OVERRIDE:
        cmd.feedrate(float(kwargs.get("value", 1.0)))
    elif verb == CommandVerb.SET_RAPID_OVERRIDE:
        cmd.rapidrate(float(kwargs.get("value", 1.0)))
    elif verb == CommandVerb.SET_SPINDLE_OVERRIDE:
        cmd.spindleoverride(float(kwargs.get("value", 1.0)), int(kwargs.get("index", 0)))
    elif verb == CommandVerb.PROGRAM_RUN:
        cmd.auto(lc.AUTO_RUN, int(kwargs.get("line", 0)))
    elif verb == CommandVerb.PROGRAM_PAUSE:
        cmd.auto(lc.AUTO_PAUSE)
    elif verb == CommandVerb.PROGRAM_RESUME:
        cmd.auto(lc.AUTO_RESUME)
    elif verb == CommandVerb.PROGRAM_STOP:
        cmd.abort()
    elif verb == CommandVerb.PROGRAM_STEP:
        cmd.auto(lc.AUTO_STEP)
    elif verb == CommandVerb.MDI:
        cmd.mdi(str(kwargs.get("command", "")))
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
    elif verb == CommandVerb.SPINDLE_STOP:
        cmd.spindle(lc.SPINDLE_OFF, 0.0, int(kwargs.get("index", 0)))
    elif verb == CommandVerb.MIST_ON:
        cmd.mist(1)
    elif verb == CommandVerb.MIST_OFF:
        cmd.mist(0)
    elif verb == CommandVerb.FLOOD_ON:
        cmd.flood(1)
    elif verb == CommandVerb.FLOOD_OFF:
        cmd.flood(0)
    else:
        raise NackError(f"unsupported verb: {verb}")


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
        # Foreign pins the client has subscribed to: name -> last seen value
        self._subscribed_foreign: dict[str, Any] = {}
        # Line-count cache for the currently-loaded g-code file. Recomputed
        # lazily in _cached_length() and only when the path changes.
        self._cached_length_path: str = ""
        self._cached_length_value: int = 0

        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # Initial snapshot (pre-poll values are fine; the first poll will
        # produce a diff that brings everything up to date).
        try:
            self._stat.poll()
        except Exception:
            pass
        self._state = stat_to_state_store(self._stat, self._linuxcnc)

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
        try:
            execute_command(self._command, self._linuxcnc, verb, kwargs)
        except NackError:
            raise
        except Exception as e:
            raise NackError(f"command {verb} failed: {e}") from e

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
                created.append(spec.name)
            try:
                self._halcomp.ready()
            except Exception as e:
                raise NackError(f"halcomp.ready() failed: {e}") from e
            self._hal_ready = True
        try:
            self._run_postgui_halfiles()
        except Exception as e:
            # POSTGUI failure is reported but does not fail the DECLARE_PINS ACK.
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

    # ----- polling thread -----

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                print(f"qtcnc-serverd: poll error: {e}", file=sys.stderr)
            self._stop_event.wait(self._poll_interval_s)

    def _poll_once(self) -> None:
        self._stat.poll()
        new_state = stat_to_state_store(self._stat, self._linuxcnc)
        # Overlay total_lines using the cached line count so the polling
        # hot-path doesn't open the file on every tick.
        length = self._cached_length(new_state.program.path)
        if length != new_state.program.total_lines:
            new_state = replace(
                new_state,
                program=replace(new_state.program, total_lines=length),
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
        for f in self._postgui_halfiles:
            path = os.path.expanduser(f)
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
            subprocess.run(argv, check=True, timeout=60)


_UNSET = object()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


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
    import os
    import signal
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    import linuxcnc as linuxcnc_module  # type: ignore
    import hal as hal_module  # type: ignore

    if args.endpoint:
        endpoint = args.endpoint
    else:
        endpoint = f"ipc:///tmp/qtcnc-{os.getuid()}"

    postgui = _load_postgui_halfiles(args.ini_path)

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
        print(f"qtcnc-serverd: fatal: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
