"""Command: typed facade over the Transport's command surface.

Each method on :class:`Command` corresponds to exactly one
``linuxcnc.command()`` dispatch. Method and parameter names mirror the
Python surface exposed by ``src/emc/usr_intf/axis/extensions/emcmodule.cc``
— zero-arg helpers take their name from the constant passed to the
dispatch (``state_estop`` == ``c.state(STATE_ESTOP)``); variadic
dispatchers take their name from the method itself (``feedrate``,
``mdi``, ``maxvel``). A reader can grep upstream LinuxCNC for any name
here and land on the C++ entry point without translation.

Widgets and handlers should never import :class:`CommandVerb`
directly — they call typed methods on :class:`Command` which forward to
``transport.exec_command``. The verb vocabulary stays private to the
wire protocol and every command gets a proper Python signature with
named parameters.

Example::

    class MyButton(QtcncWidget, QPushButton):
        def on_clicked(self):
            self.window().qtcnc_command.state_estop()

:class:`Command` is a plain Python class, not a QObject — it doesn't
emit signals and nothing connects to it. Failures raise
:class:`NackError` (or a subclass) which callers can catch if they
want to show a dialog.

This module also owns ``_VERB_TO_PREDICATE``, the canonical mapping
from :class:`CommandVerb` to ``(state_predicate, nack_reason)``. Both
the daemon's ``execute_command`` guard table and the client-side
:meth:`Command.precheck` helper read from the same dict so they cannot
drift. Verbs absent from the dict are unconditionally permitted.
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from qtcnc.core.state import StateStore
from qtcnc.core.types import GetToolDbResult, MotionMode, TaskMode
from qtcnc.signals import CommandVerb
from qtcnc.transport.base import Transport

if TYPE_CHECKING:
    from qtcnc.core.status import Status


_VERB_TO_PREDICATE: dict[CommandVerb, tuple[Callable[[StateStore], bool], str]] = {
    CommandVerb.STATE_ON: (
        lambda s: not s.machine.estop,
        "state_on_requires_estop_reset",
    ),
    CommandVerb.SET_MODE: (
        lambda s: s.machine.is_ready and s.machine.is_idle,
        "set_mode_requires_ready_idle",
    ),
    CommandVerb.HOME: (
        lambda s: s.machine.can_home,
        "home_requires_manual_ready",
    ),
    CommandVerb.UNHOME: (
        lambda s: s.machine.can_unhome,
        "unhome_requires_manual_ready",
    ),
    CommandVerb.JOG_CONTINUOUS: (
        lambda s: s.machine.can_jog,
        "jog_requires_manual_idle_ready",
    ),
    CommandVerb.JOG_STOP: (
        lambda s: s.machine.is_ready,
        "jog_stop_requires_ready",
    ),
    CommandVerb.JOG_INCREMENT: (
        lambda s: s.machine.can_jog,
        "jog_requires_manual_idle_ready",
    ),
    CommandVerb.AUTO_RUN: (
        lambda s: s.can_run_program,
        "auto_run_requires_auto_idle_loaded",
    ),
    CommandVerb.AUTO_PAUSE: (
        lambda s: s.can_pause_program,
        "auto_pause_requires_running",
    ),
    CommandVerb.AUTO_RESUME: (
        lambda s: s.can_resume_program,
        "auto_resume_requires_paused_auto",
    ),
    CommandVerb.ABORT: (
        lambda s: s.can_abort_program,
        "abort_requires_ready",
    ),
    CommandVerb.AUTO_STEP: (
        lambda s: s.can_step_program,
        "auto_step_requires_auto_ready",
    ),
    CommandVerb.MDI: (
        lambda s: s.can_mdi,
        "mdi_requires_mdi_idle_ready",
    ),
    CommandVerb.SPINDLE_FORWARD: (
        lambda s: s.machine.can_spindle,
        "spindle_requires_ready",
    ),
    CommandVerb.SPINDLE_REVERSE: (
        lambda s: s.machine.can_spindle,
        "spindle_requires_ready",
    ),
    CommandVerb.MIST_ON: (
        lambda s: s.machine.is_ready,
        "coolant_requires_ready",
    ),
    CommandVerb.MIST_OFF: (
        lambda s: s.machine.is_ready,
        "coolant_requires_ready",
    ),
    CommandVerb.FLOOD_ON: (
        lambda s: s.machine.is_ready,
        "coolant_requires_ready",
    ),
    CommandVerb.FLOOD_OFF: (
        lambda s: s.machine.is_ready,
        "coolant_requires_ready",
    ),
    CommandVerb.TRAJ_MODE: (
        lambda s: s.machine.is_ready and s.machine.is_idle,
        "traj_mode_requires_ready_idle",
    ),
    CommandVerb.TOOL_OFFSET: (
        lambda s: s.machine.is_ready,
        "tool_offset_requires_ready",
    ),
    CommandVerb.BRAKE_ENGAGE: (
        lambda s: s.machine.is_ready,
        "brake_requires_ready",
    ),
    CommandVerb.BRAKE_RELEASE: (
        lambda s: s.machine.is_ready,
        "brake_requires_ready",
    ),
    CommandVerb.LOAD_TOOL_TABLE: (
        lambda s: s.program.is_editable,
        "load_tool_table_requires_idle",
    ),
    CommandVerb.OVERRIDE_LIMITS: (
        lambda s: s.machine.is_manual,
        "override_limits_requires_manual",
    ),
    CommandVerb.RESET_INTERPRETER: (
        lambda s: s.machine.is_ready,
        "reset_interpreter_requires_ready",
    ),
    CommandVerb.AUTO_REVERSE: (
        lambda s: s.machine.is_auto and s.machine.is_ready,
        "auto_reverse_requires_auto_ready",
    ),
    CommandVerb.AUTO_FORWARD: (
        lambda s: s.machine.is_auto and s.machine.is_ready,
        "auto_forward_requires_auto_ready",
    ),
    CommandVerb.SET_MIN_LIMIT: (
        lambda s: s.machine.is_manual and s.machine.is_idle,
        "limits_require_manual_idle",
    ),
    CommandVerb.SET_MAX_LIMIT: (
        lambda s: s.machine.is_manual and s.machine.is_idle,
        "limits_require_manual_idle",
    ),
    CommandVerb.SPINDLE_INCREASE: (
        lambda s: s.machine.can_spindle,
        "spindle_requires_ready",
    ),
    CommandVerb.SPINDLE_DECREASE: (
        lambda s: s.machine.can_spindle,
        "spindle_requires_ready",
    ),
    CommandVerb.SPINDLE_CONSTANT: (
        lambda s: s.machine.can_spindle,
        "spindle_requires_ready",
    ),
}


def _precheck(state: StateStore, verb: CommandVerb) -> str | None:
    """Return None if `state` permits `verb`, or the NackError reason
    string that the daemon's guard table would raise."""
    entry = _VERB_TO_PREDICATE.get(verb)
    if entry is None:
        return None
    predicate, reason = entry
    return None if predicate(state) else reason


class Command:
    """Typed command dispatcher. One per client."""

    def __init__(
        self, transport: Transport, *, status: "Status | None" = None,
    ) -> None:
        self._transport = transport
        self._status = status

    def precheck(self, verb: CommandVerb) -> str | None:
        """Return None if the current client-side state permits `verb`,
        or a NackError reason string explaining the refusal. Widgets may
        call this before firing a command to gate buttons without
        waiting for the daemon's reply. The daemon remains the
        authoritative refusal path.
        """
        if self._status is None:
            return None
        return _precheck(self._status.state, verb)

    # --- cmd.state(lc.STATE_*) — emcmodule.cc:3148-3151 ---

    def state_estop(self) -> None:
        """``cmd.state(lc.STATE_ESTOP)``."""
        self._transport.exec_command(CommandVerb.STATE_ESTOP)

    def state_estop_reset(self) -> None:
        """``cmd.state(lc.STATE_ESTOP_RESET)``."""
        self._transport.exec_command(CommandVerb.STATE_ESTOP_RESET)

    def state_on(self) -> None:
        """``cmd.state(lc.STATE_ON)``."""
        self._transport.exec_command(CommandVerb.STATE_ON)

    def state_off(self) -> None:
        """``cmd.state(lc.STATE_OFF)``."""
        self._transport.exec_command(CommandVerb.STATE_OFF)

    # --- cmd.mode(lc.MODE_*) — emcmodule.cc:3144-3146 ---

    def mode(self, task_mode: TaskMode) -> None:
        """``cmd.mode(lc.MODE_{MANUAL|AUTO|MDI})``."""
        self._transport.exec_command(CommandVerb.SET_MODE, mode=task_mode)

    # --- cmd.home / cmd.unhome — emcmodule.cc:Command_methods ---

    def home(self, joint: int) -> None:
        """``cmd.home(joint)``. Pass ``joint=-1`` to home every joint."""
        self._transport.exec_command(CommandVerb.HOME, joint=int(joint))

    def unhome(self, joint: int) -> None:
        """``cmd.unhome(joint)``. Pass ``joint=-1`` to unhome every joint."""
        self._transport.exec_command(CommandVerb.UNHOME, joint=int(joint))

    # --- cmd.jog(lc.JOG_*, jjogmode, …) — emcmodule.cc:3169-3171 ---

    def jog_continuous(
        self, axis: int, velocity: float, *, joint: int = -1,
    ) -> None:
        """``cmd.jog(lc.JOG_CONTINUOUS, jjogmode, axis_or_joint, velocity)``.

        `axis` is the axis index (0=X..8=W) used in teleop mode.
        `joint` is the joint number used in free mode; -1 means the
        daemon derives it from `axis` via the coordinates mapping.
        """
        self._transport.exec_command(
            CommandVerb.JOG_CONTINUOUS,
            axis=int(axis), velocity=float(velocity), joint=int(joint),
        )

    def jog_stop(self, axis: int, *, joint: int = -1) -> None:
        """``cmd.jog(lc.JOG_STOP, jjogmode, axis_or_joint)``."""
        self._transport.exec_command(
            CommandVerb.JOG_STOP, axis=int(axis), joint=int(joint),
        )

    def jog_increment(
        self, axis: int, distance: float, velocity: float, *, joint: int = -1,
    ) -> None:
        """``cmd.jog(lc.JOG_INCREMENT, jjogmode, axis_or_joint, velocity, distance)``."""
        self._transport.exec_command(
            CommandVerb.JOG_INCREMENT,
            axis=int(axis), distance=float(distance),
            velocity=float(velocity), joint=int(joint),
        )

    # --- cmd.feedrate / rapidrate / spindleoverride — emcmodule.cc:1491-1505 ---

    def feedrate(self, scale: float) -> None:
        """``cmd.feedrate(scale)`` — set the feed-rate scale factor."""
        self._transport.exec_command(CommandVerb.FEEDRATE, value=float(scale))

    def rapidrate(self, scale: float) -> None:
        """``cmd.rapidrate(scale)`` — set the rapid-rate scale factor."""
        self._transport.exec_command(CommandVerb.RAPIDRATE, value=float(scale))

    def spindleoverride(self, scale: float, index: int = 0) -> None:
        """``cmd.spindleoverride(scale, spindle=0)``."""
        self._transport.exec_command(
            CommandVerb.SPINDLEOVERRIDE, index=int(index), value=float(scale),
        )

    # --- cmd.auto(lc.AUTO_*) — emcmodule.cc:3173-3178 ---

    def auto_run(self) -> None:
        """``cmd.auto(lc.AUTO_RUN)`` — start the loaded program."""
        self._transport.exec_command(CommandVerb.AUTO_RUN)

    def auto_pause(self) -> None:
        """``cmd.auto(lc.AUTO_PAUSE)``."""
        self._transport.exec_command(CommandVerb.AUTO_PAUSE)

    def auto_resume(self) -> None:
        """``cmd.auto(lc.AUTO_RESUME)``."""
        self._transport.exec_command(CommandVerb.AUTO_RESUME)

    def auto_step(self) -> None:
        """``cmd.auto(lc.AUTO_STEP)``."""
        self._transport.exec_command(CommandVerb.AUTO_STEP)

    def auto_reverse(self) -> None:
        """``cmd.auto(lc.AUTO_REVERSE)`` — flip the planner direction
        during a pause (rigid tapping recovery)."""
        self._transport.exec_command(CommandVerb.AUTO_REVERSE)

    def auto_forward(self) -> None:
        """``cmd.auto(lc.AUTO_FORWARD)`` — resume forward planner
        direction during a pause."""
        self._transport.exec_command(CommandVerb.AUTO_FORWARD)

    # --- cmd.abort / cmd.mdi — emcmodule.cc:Command_methods ---

    def abort(self) -> None:
        """``cmd.abort()`` — halt any in-flight motion or interp."""
        self._transport.exec_command(CommandVerb.ABORT)

    def mdi(self, command: str) -> None:
        """``cmd.mdi(text)`` — execute one interp line in MDI mode."""
        self._transport.exec_command(CommandVerb.MDI, command=str(command))

    # --- cmd.spindle(lc.SPINDLE_*, …) — emcmodule.cc:3153-3158 ---

    def spindle_forward(self, speed: float, index: int = 0) -> None:
        """``cmd.spindle(lc.SPINDLE_FORWARD, speed, spindle=0)``."""
        self._transport.exec_command(
            CommandVerb.SPINDLE_FORWARD, index=int(index), speed=float(speed),
        )

    def spindle_reverse(self, speed: float, index: int = 0) -> None:
        """``cmd.spindle(lc.SPINDLE_REVERSE, speed, spindle=0)``."""
        self._transport.exec_command(
            CommandVerb.SPINDLE_REVERSE, index=int(index), speed=float(speed),
        )

    def spindle_off(self, index: int = 0) -> None:
        """``cmd.spindle(lc.SPINDLE_OFF, spindle=0)``."""
        self._transport.exec_command(CommandVerb.SPINDLE_OFF, index=int(index))

    def spindle_increase(self, index: int = 0) -> None:
        """``cmd.spindle(lc.SPINDLE_INCREASE, spindle=0)``."""
        self._transport.exec_command(CommandVerb.SPINDLE_INCREASE, index=int(index))

    def spindle_decrease(self, index: int = 0) -> None:
        """``cmd.spindle(lc.SPINDLE_DECREASE, spindle=0)``."""
        self._transport.exec_command(CommandVerb.SPINDLE_DECREASE, index=int(index))

    def spindle_constant(self, index: int = 0) -> None:
        """``cmd.spindle(lc.SPINDLE_CONSTANT, spindle=0)``."""
        self._transport.exec_command(CommandVerb.SPINDLE_CONSTANT, index=int(index))

    # --- cmd.mist / cmd.flood — emcmodule.cc:3160-3164 ---

    def mist_on(self) -> None:
        """``cmd.mist(lc.MIST_ON)``."""
        self._transport.exec_command(CommandVerb.MIST_ON)

    def mist_off(self) -> None:
        """``cmd.mist(lc.MIST_OFF)``."""
        self._transport.exec_command(CommandVerb.MIST_OFF)

    def flood_on(self) -> None:
        """``cmd.flood(lc.FLOOD_ON)``."""
        self._transport.exec_command(CommandVerb.FLOOD_ON)

    def flood_off(self) -> None:
        """``cmd.flood(lc.FLOOD_OFF)``."""
        self._transport.exec_command(CommandVerb.FLOOD_OFF)

    # --- cmd.brake(lc.BRAKE_*) — emcmodule.cc:3166-3167 ---

    def brake_engage(self, index: int = 0) -> None:
        """``cmd.brake(lc.BRAKE_ENGAGE, spindle=0)``."""
        self._transport.exec_command(CommandVerb.BRAKE_ENGAGE, index=int(index))

    def brake_release(self, index: int = 0) -> None:
        """``cmd.brake(lc.BRAKE_RELEASE, spindle=0)``."""
        self._transport.exec_command(CommandVerb.BRAKE_RELEASE, index=int(index))

    # --- Stand-alone cmd methods — emcmodule.cc:Command_methods (2077-2139) ---

    def debug(self, mask: int) -> None:
        """``cmd.debug(mask)`` — set interpreter debug bitmask."""
        self._transport.exec_command(CommandVerb.DEBUG, mask=int(mask))

    def traj_mode(self, motion_mode: MotionMode) -> None:
        """``cmd.traj_mode(lc.TRAJ_MODE_{FREE|COORD|TELEOP})``."""
        self._transport.exec_command(CommandVerb.TRAJ_MODE, mode=motion_mode)

    def maxvel(self, value: float) -> None:
        """``cmd.maxvel(value)`` — operator-settable max trajectory velocity."""
        self._transport.exec_command(CommandVerb.MAXVEL, value=float(value))

    def tool_offset(
        self,
        tool: int,
        *,
        zoffset: float = 0.0,
        xoffset: float = 0.0,
        diameter: float = 0.0,
        frontangle: float = 0.0,
        backangle: float = 0.0,
        orientation: int = 0,
    ) -> None:
        """``cmd.tool_offset(tool, zoffset, xoffset, diameter,
        frontangle, backangle, orientation)``."""
        self._transport.exec_command(
            CommandVerb.TOOL_OFFSET,
            tool=int(tool),
            zoffset=float(zoffset),
            xoffset=float(xoffset),
            diameter=float(diameter),
            frontangle=float(frontangle),
            backangle=float(backangle),
            orientation=int(orientation),
        )

    def load_tool_table(self) -> None:
        """``cmd.load_tool_table()`` — re-read ``tool.tbl`` from disk."""
        self._transport.exec_command(CommandVerb.LOAD_TOOL_TABLE)

    def task_plan_synch(self) -> None:
        """``cmd.task_plan_synch()`` — sync interpreter with task state."""
        self._transport.exec_command(CommandVerb.TASK_PLAN_SYNCH)

    def override_limits(self) -> None:
        """``cmd.override_limits()`` — temporarily suspend hard-limit checks."""
        self._transport.exec_command(CommandVerb.OVERRIDE_LIMITS)

    def reset_interpreter(self) -> None:
        """``cmd.reset_interpreter()`` — reset the interpreter to initial state."""
        self._transport.exec_command(CommandVerb.RESET_INTERPRETER)

    def set_optional_stop(self, enabled: bool) -> None:
        """``cmd.set_optional_stop(bool)`` — honour M1 optional stops."""
        self._transport.exec_command(
            CommandVerb.SET_OPTIONAL_STOP, enabled=bool(enabled),
        )

    def set_block_delete(self, enabled: bool) -> None:
        """``cmd.set_block_delete(bool)`` — skip lines beginning with ``/``."""
        self._transport.exec_command(
            CommandVerb.SET_BLOCK_DELETE, enabled=bool(enabled),
        )

    def set_min_limit(self, joint: int, value: float) -> None:
        """``cmd.set_min_limit(joint, value)``."""
        self._transport.exec_command(
            CommandVerb.SET_MIN_LIMIT, joint=int(joint), value=float(value),
        )

    def set_max_limit(self, joint: int, value: float) -> None:
        """``cmd.set_max_limit(joint, value)``."""
        self._transport.exec_command(
            CommandVerb.SET_MAX_LIMIT, joint=int(joint), value=float(value),
        )

    def set_feed_override(self, enabled: bool) -> None:
        """``cmd.set_feed_override(bool)`` — enable/disable feed override."""
        self._transport.exec_command(
            CommandVerb.SET_FEED_OVERRIDE, enabled=bool(enabled),
        )

    def set_spindle_override(self, enabled: bool, index: int = 0) -> None:
        """``cmd.set_spindle_override(bool, spindle=0)`` — enable/disable
        spindle override."""
        self._transport.exec_command(
            CommandVerb.SET_SPINDLE_OVERRIDE,
            index=int(index),
            enabled=bool(enabled),
        )

    def set_feed_hold(self, enabled: bool) -> None:
        """``cmd.set_feed_hold(bool)``."""
        self._transport.exec_command(
            CommandVerb.SET_FEED_HOLD, enabled=bool(enabled),
        )

    def set_adaptive_feed(self, enabled: bool) -> None:
        """``cmd.set_adaptive_feed(bool)``."""
        self._transport.exec_command(
            CommandVerb.SET_ADAPTIVE_FEED, enabled=bool(enabled),
        )

    def set_digital_output(self, index: int, value: bool) -> None:
        """``cmd.set_digital_output(index, bool)``."""
        self._transport.exec_command(
            CommandVerb.SET_DIGITAL_OUTPUT, index=int(index), value=bool(value),
        )

    def set_analog_output(self, index: int, value: float) -> None:
        """``cmd.set_analog_output(index, value)``."""
        self._transport.exec_command(
            CommandVerb.SET_ANALOG_OUTPUT, index=int(index), value=float(value),
        )

    def error_msg(self, text: str) -> None:
        """``cmd.error_msg(text)`` — inject into the error channel."""
        self._transport.exec_command(CommandVerb.ERROR_MSG, text=str(text))

    def text_msg(self, text: str) -> None:
        """``cmd.text_msg(text)`` — inject a text message into the error channel."""
        self._transport.exec_command(CommandVerb.TEXT_MSG, text=str(text))

    def display_msg(self, text: str) -> None:
        """``cmd.display_msg(text)`` — inject a display message into the error channel."""
        self._transport.exec_command(CommandVerb.DISPLAY_MSG, text=str(text))

    # --- transport passthrough: file / HAL ---

    def load_program(self, path: str) -> None:
        self._transport.load_program(path)

    def write_pin(self, name: str, value: Any) -> None:
        self._transport.write_pin(name, value)

    def subscribe_pin(self, name: str) -> None:
        self._transport.subscribe_pin(name)

    # --- tool database ---

    def get_tool_db(self) -> GetToolDbResult:
        return self._transport.get_tool_db()

    def add_tool(self, tool_id: int, pocket: int, **fields: Any) -> None:
        self._transport.add_tool(tool_id, pocket, **fields)

    def remove_tool(self, tool_id: int) -> None:
        self._transport.remove_tool(tool_id)

    def update_tool(self, tool_id: int, **fields: Any) -> None:
        self._transport.update_tool(tool_id, **fields)

    # --- program metadata ---

    def set_program_tools(self, tools: frozenset[int]) -> None:
        """Tell the daemon which tools the loaded program references.

        The daemon stores this on ProgramState.requested_tools so
        can_run_program blocks execution when tools are missing from
        the tool table.
        """
        self._transport.exec_command(
            CommandVerb.SET_PROGRAM_TOOLS,
            tools=sorted(tools),
        )
