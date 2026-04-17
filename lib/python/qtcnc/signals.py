"""Protocol and signal-name constants.

Single source of truth for the strings that cross process and network
boundaries. Both client and server import from here so a typo in one
place is caught as an AttributeError instead of a silent mismatch.

`CommandVerb` entries are named to match the `linuxcnc.command()` Python
surface exposed by ``src/emc/usr_intf/axis/extensions/emcmodule.cc``:
either the method name (``feedrate``, ``mdi``, ``abort``) or the integer
constant passed as first argument to a dispatch method (``STATE_ESTOP``
via ``cmd.state``, ``AUTO_RUN`` via ``cmd.auto``, ``JOG_CONTINUOUS`` via
``cmd.jog``, ``SPINDLE_FORWARD`` via ``cmd.spindle``). Greppability in
upstream LinuxCNC is the goal — each verb name appears verbatim in
``emcmodule.cc`` so a reader can find the underlying C++ implementation
without translation.
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
    # --- tool database REQ/REP ---
    GET_TOOL_DB = "get_tool_db"
    ADD_TOOL = "add_tool"
    REMOVE_TOOL = "remove_tool"
    UPDATE_TOOL = "update_tool"
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
    """Sub-types carried inside an EXEC_COMMAND payload.

    Each name mirrors a symbol exported by the ``linuxcnc`` Python
    module. Entries that map to a ``cmd.state()`` / ``cmd.mode()`` /
    ``cmd.auto()`` / ``cmd.jog()`` / ``cmd.spindle()`` / ``cmd.mist()`` /
    ``cmd.flood()`` / ``cmd.brake()`` dispatch take their name from the
    integer constant passed as the first argument; stand-alone methods
    take their name from the method itself (``feedrate``, ``mdi``,
    ``abort``, ``debug``, ``maxvel``, ``tool_offset``, …).
    """

    # cmd.state(lc.STATE_*) — emcmodule.cc:3148-3151
    STATE_ESTOP = "state_estop"
    STATE_ESTOP_RESET = "state_estop_reset"
    STATE_ON = "state_on"
    STATE_OFF = "state_off"
    # cmd.mode(lc.MODE_*) — emcmodule.cc:3144-3146
    SET_MODE = "set_mode"
    # cmd.home(joint) / cmd.unhome(joint) — emcmodule.cc:Command_methods
    HOME = "home"
    UNHOME = "unhome"
    # cmd.jog(lc.JOG_*, jjogmode, …) — emcmodule.cc:3169-3171
    JOG_CONTINUOUS = "jog_continuous"
    JOG_STOP = "jog_stop"
    JOG_INCREMENT = "jog_increment"
    # cmd.feedrate / rapidrate / spindleoverride — emcmodule.cc:1491-1505
    FEEDRATE = "feedrate"
    RAPIDRATE = "rapidrate"
    SPINDLEOVERRIDE = "spindleoverride"
    # cmd.auto(lc.AUTO_*) — emcmodule.cc:3173-3178
    AUTO_RUN = "auto_run"
    AUTO_PAUSE = "auto_pause"
    AUTO_RESUME = "auto_resume"
    AUTO_STEP = "auto_step"
    AUTO_REVERSE = "auto_reverse"
    AUTO_FORWARD = "auto_forward"
    # cmd.abort() / cmd.mdi(text) — emcmodule.cc:Command_methods
    ABORT = "abort"
    MDI = "mdi"
    # cmd.spindle(lc.SPINDLE_*, …) — emcmodule.cc:3153-3158
    SPINDLE_FORWARD = "spindle_forward"
    SPINDLE_REVERSE = "spindle_reverse"
    SPINDLE_OFF = "spindle_off"
    SPINDLE_INCREASE = "spindle_increase"
    SPINDLE_DECREASE = "spindle_decrease"
    SPINDLE_CONSTANT = "spindle_constant"
    # cmd.mist(lc.MIST_*) / cmd.flood(lc.FLOOD_*) — emcmodule.cc:3160-3164
    MIST_ON = "mist_on"
    MIST_OFF = "mist_off"
    FLOOD_ON = "flood_on"
    FLOOD_OFF = "flood_off"
    # cmd.brake(lc.BRAKE_*) — emcmodule.cc:3166-3167
    BRAKE_ENGAGE = "brake_engage"
    BRAKE_RELEASE = "brake_release"
    # Stand-alone cmd methods — emcmodule.cc:Command_methods (2077-2139)
    DEBUG = "debug"
    TRAJ_MODE = "traj_mode"
    MAXVEL = "maxvel"
    TOOL_OFFSET = "tool_offset"
    LOAD_TOOL_TABLE = "load_tool_table"
    TASK_PLAN_SYNCH = "task_plan_synch"
    OVERRIDE_LIMITS = "override_limits"
    RESET_INTERPRETER = "reset_interpreter"
    SET_OPTIONAL_STOP = "set_optional_stop"
    SET_BLOCK_DELETE = "set_block_delete"
    SET_MIN_LIMIT = "set_min_limit"
    SET_MAX_LIMIT = "set_max_limit"
    SET_FEED_OVERRIDE = "set_feed_override"
    SET_SPINDLE_OVERRIDE = "set_spindle_override"
    SET_FEED_HOLD = "set_feed_hold"
    SET_ADAPTIVE_FEED = "set_adaptive_feed"
    SET_DIGITAL_OUTPUT = "set_digital_output"
    SET_ANALOG_OUTPUT = "set_analog_output"
    ERROR_MSG = "error_msg"
    TEXT_MSG = "text_msg"
    DISPLAY_MSG = "display_msg"
    # qtcnc-internal state verbs (no linuxcnc.command() equivalent)
    SET_PROGRAM_TOOLS = "set_program_tools"


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
