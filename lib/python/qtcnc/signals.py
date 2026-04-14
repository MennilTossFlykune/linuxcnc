"""Protocol and signal-name constants.

Single source of truth for the strings that cross process and network
boundaries. Both client and server import from here so a typo in one
place is caught as an AttributeError instead of a silent mismatch.
"""

from __future__ import annotations

from enum import StrEnum


class MessageType(StrEnum):
    """Wire message tags for REQ/REP and PUB/SUB envelopes."""

    # --- REQ/REP ---
    HELLO = "hello"
    WELCOME = "welcome"
    PING = "ping"
    PONG = "pong"
    GET_SNAPSHOT = "get_snapshot"
    SNAPSHOT = "snapshot"
    EXEC_COMMAND = "exec_command"
    LOAD_PROGRAM = "load_program"
    DECLARE_PINS = "declare_pins"
    WRITE_PIN = "write_pin"
    SUBSCRIBE_PIN = "subscribe_pin"
    ACK = "ack"
    NACK = "nack"
    BYE = "bye"
    # --- PUB/SUB (published with a topic frame 0) ---
    STATE_DIFF = "state_diff"
    HAL_PIN_UPDATE = "hal_pin_update"
    ERROR = "error"
    LIFECYCLE = "lifecycle"


class Topic(StrEnum):
    """PUB/SUB topic names used as frame 0 of multipart messages."""

    STATE_DIFF = "state.diff"
    STATE_SNAPSHOT = "state.snapshot"
    HAL_PIN = "hal.pin"
    ERROR = "error"
    LIFECYCLE = "lifecycle"


class CommandVerb(StrEnum):
    """Sub-types carried inside an EXEC_COMMAND payload."""

    ESTOP = "estop"
    ESTOP_RESET = "estop_reset"
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    SET_MODE = "set_mode"
    HOME_AXIS = "home_axis"
    UNHOME_AXIS = "unhome_axis"
    HOME_ALL = "home_all"
    JOG_START = "jog_start"
    JOG_STOP = "jog_stop"
    JOG_INCREMENT = "jog_increment"
    SET_FEED_OVERRIDE = "set_feed_override"
    SET_RAPID_OVERRIDE = "set_rapid_override"
    SET_SPINDLE_OVERRIDE = "set_spindle_override"
    PROGRAM_RUN = "program_run"
    PROGRAM_PAUSE = "program_pause"
    PROGRAM_RESUME = "program_resume"
    PROGRAM_STOP = "program_stop"
    PROGRAM_STEP = "program_step"
    MDI = "mdi"
    SPINDLE_FORWARD = "spindle_forward"
    SPINDLE_REVERSE = "spindle_reverse"
    SPINDLE_STOP = "spindle_stop"
    MIST_ON = "mist_on"
    MIST_OFF = "mist_off"
    FLOOD_ON = "flood_on"
    FLOOD_OFF = "flood_off"


class Lifecycle(StrEnum):
    """Payload tags for LIFECYCLE topic messages."""

    DAEMON_READY = "daemon_ready"
    DAEMON_SHUTDOWN = "daemon_shutdown"
    PROGRAM_LOADING = "program_loading"
    PROGRAM_LOADED = "program_loaded"
    PROGRAM_LOAD_FAILED = "program_load_failed"
    PROGRAM_CLOSED = "program_closed"
    PROGRAM_MISSING = "program_missing"
    PROGRAM_STARTED = "program_started"
    PROGRAM_PAUSED = "program_paused"
    PROGRAM_FINISHED = "program_finished"
