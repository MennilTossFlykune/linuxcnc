"""Command: typed facade over the Transport's command surface.

Widgets and handlers should never import `CommandVerb` directly — they
call typed methods on `Command` which forwards to `transport.exec_command`.
This keeps the verb vocabulary private to the wire protocol and gives
each command a proper Python signature with named parameters.

Example:

    class EstopButton(QtcncWidget):
        def on_clicked(self):
            self.window().qtcnc_command.estop()

`Command` is a plain Python class, not a QObject — it doesn't emit
signals and nothing connects to it. Failures raise `NackError` (or
subclass) which callers can catch if they want to show a dialog.
"""

from __future__ import annotations

from typing import Any

from qtcnc.core.types import TaskMode
from qtcnc.signals import CommandVerb
from qtcnc.transport.base import Transport


class Command:
    """Typed command dispatcher. One per client."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    # --- machine power ---

    def estop(self) -> None:
        self._transport.exec_command(CommandVerb.ESTOP)

    def estop_reset(self) -> None:
        self._transport.exec_command(CommandVerb.ESTOP_RESET)

    def power_on(self) -> None:
        self._transport.exec_command(CommandVerb.POWER_ON)

    def power_off(self) -> None:
        self._transport.exec_command(CommandVerb.POWER_OFF)

    # --- mode / homing ---

    def set_mode(self, mode: TaskMode) -> None:
        self._transport.exec_command(CommandVerb.SET_MODE, mode=mode)

    def home_axis(self, axis: int) -> None:
        self._transport.exec_command(CommandVerb.HOME_AXIS, axis=int(axis))

    def unhome_axis(self, axis: int) -> None:
        self._transport.exec_command(CommandVerb.UNHOME_AXIS, axis=int(axis))

    def home_all(self) -> None:
        self._transport.exec_command(CommandVerb.HOME_ALL)

    # --- jogging ---

    def jog_start(self, axis: int, velocity: float) -> None:
        self._transport.exec_command(
            CommandVerb.JOG_START, axis=int(axis), velocity=float(velocity),
        )

    def jog_stop(self, axis: int) -> None:
        self._transport.exec_command(CommandVerb.JOG_STOP, axis=int(axis))

    def jog_increment(self, axis: int, distance: float, velocity: float) -> None:
        self._transport.exec_command(
            CommandVerb.JOG_INCREMENT,
            axis=int(axis),
            distance=float(distance),
            velocity=float(velocity),
        )

    # --- overrides ---

    def set_feed_override(self, value: float) -> None:
        self._transport.exec_command(CommandVerb.SET_FEED_OVERRIDE, value=float(value))

    def set_rapid_override(self, value: float) -> None:
        self._transport.exec_command(CommandVerb.SET_RAPID_OVERRIDE, value=float(value))

    def set_spindle_override(self, value: float, index: int = 0) -> None:
        self._transport.exec_command(
            CommandVerb.SET_SPINDLE_OVERRIDE, index=int(index), value=float(value),
        )

    # --- program control ---

    def program_run(self) -> None:
        self._transport.exec_command(CommandVerb.PROGRAM_RUN)

    def program_pause(self) -> None:
        self._transport.exec_command(CommandVerb.PROGRAM_PAUSE)

    def program_resume(self) -> None:
        self._transport.exec_command(CommandVerb.PROGRAM_RESUME)

    def program_stop(self) -> None:
        self._transport.exec_command(CommandVerb.PROGRAM_STOP)

    def program_step(self) -> None:
        self._transport.exec_command(CommandVerb.PROGRAM_STEP)

    def mdi(self, command: str) -> None:
        self._transport.exec_command(CommandVerb.MDI, command=command)

    # --- spindle ---

    def spindle_forward(self, speed: float, index: int = 0) -> None:
        self._transport.exec_command(
            CommandVerb.SPINDLE_FORWARD, index=int(index), speed=float(speed),
        )

    def spindle_reverse(self, speed: float, index: int = 0) -> None:
        self._transport.exec_command(
            CommandVerb.SPINDLE_REVERSE, index=int(index), speed=float(speed),
        )

    def spindle_stop(self, index: int = 0) -> None:
        self._transport.exec_command(CommandVerb.SPINDLE_STOP, index=int(index))

    # --- coolant ---

    def mist_on(self) -> None:
        self._transport.exec_command(CommandVerb.MIST_ON)

    def mist_off(self) -> None:
        self._transport.exec_command(CommandVerb.MIST_OFF)

    def flood_on(self) -> None:
        self._transport.exec_command(CommandVerb.FLOOD_ON)

    def flood_off(self) -> None:
        self._transport.exec_command(CommandVerb.FLOOD_OFF)

    # --- file / HAL ---

    def load_program(self, path: str) -> None:
        self._transport.load_program(path)

    def write_pin(self, name: str, value: Any) -> None:
        self._transport.write_pin(name, value)

    def subscribe_pin(self, name: str) -> None:
        self._transport.subscribe_pin(name)
