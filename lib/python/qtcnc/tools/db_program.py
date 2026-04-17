"""qtcnc tool database — DB_PROGRAM implementation.

Implements the v2.1 stdin/stdout protocol expected by
``src/emc/tooldata/tooldata_db.cc``. Uses sqlite via
``qtcnc.tools.tooldb_schema`` as the backing store.

INI usage::

    [EMCIO]
    DB_PROGRAM = python3 -m qtcnc.tools.db_program /path/to/tools.db
    RANDOM_TOOLCHANGER = 0   ; or 1

The database file path is the first positional argument. If omitted,
defaults to ``tools.db`` in the current directory.

Stdout is piped to LinuxCNC — use stderr for diagnostics.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

import tooldb
from tooldb import tooldb_callbacks, tooldb_tools

from qtcnc.tools.tooldb_schema import (
    delete_tool,
    format_toolline,
    get_all_tools,
    get_spindle_state,
    get_tool,
    next_available_pocket,
    open_db,
    parse_toolline,
    set_spindle_state,
    swap_pockets,
    tool_ids,
    upsert_tool,
)

_mutex = threading.Lock()
_conn = None
_random_toolchanger = False


_db_log = logging.getLogger("qtcnc.tools.db_program")


def _log(msg: str) -> None:
    _db_log.info(msg)


def _user_get_tool(toolno: int) -> str:
    with _mutex:
        row = get_tool(_conn, toolno)
        if row is None:
            raise KeyError(f"tool {toolno} not in database")
        return format_toolline(row)


def _user_put_tool(toolno: int, toolline: str) -> None:
    with _mutex:
        fields = parse_toolline(toolline)
        fields.pop("tool_id", None)
        upsert_tool(_conn, toolno, **fields)


def _user_load_spindle(toolno: int, toolline: str) -> None:
    """Handle 'l' command — load a tool into the spindle.

    Non-random: ``l T<n> P0`` — tool goes to pocket 0 (spindle).
    Random: ``l T<n> P0`` — second of a paired load; the preceding
    unload already moved the old tool out.
    """
    with _mutex:
        fields = parse_toolline(toolline)
        pocket_from_cmd = fields.get("pocket", 0)

        if _random_toolchanger:
            row = get_tool(_conn, toolno)
            if row is not None:
                old_pocket = row["pocket"]
                if old_pocket != 0:
                    _conn.execute(
                        "UPDATE tool SET pocket = 0 WHERE tool_id = ?",
                        (toolno,),
                    )
                    _conn.commit()
        else:
            occupant = _conn.execute(
                "SELECT tool_id FROM tool WHERE pocket = 0 AND tool_id != ?",
                (toolno,),
            ).fetchone()
            if occupant is not None:
                _conn.execute(
                    "UPDATE tool SET pocket = tool_id WHERE tool_id = ?",
                    (occupant[0],),
                )
            _conn.execute(
                "UPDATE tool SET pocket = 0 WHERE tool_id = ?",
                (toolno,),
            )
            _conn.commit()

        set_spindle_state(_conn, toolno)


def _user_unload_spindle(toolno: int, toolline: str) -> None:
    """Handle 'u' command — unload a tool from the spindle.

    Non-random: ``u T0 P0`` — tool returns to its home pocket.
    Random: ``u T<old> P<dest>`` — old tool goes to the pocket the
    new tool was just fetched from.
    """
    with _mutex:
        fields = parse_toolline(toolline)
        dest_pocket = fields.get("pocket", 0)

        current_spindle = get_spindle_state(_conn)

        if _random_toolchanger:
            if toolno > 0 and dest_pocket > 0:
                occupant = _conn.execute(
                    "SELECT tool_id FROM tool WHERE pocket = ?",
                    (dest_pocket,),
                ).fetchone()
                if occupant is not None:
                    _conn.execute(
                        "UPDATE tool SET pocket = -1 WHERE tool_id = ?",
                        (occupant[0],),
                    )
                _conn.execute(
                    "UPDATE tool SET pocket = ? WHERE tool_id = ?",
                    (dest_pocket, toolno),
                )
                _conn.commit()
        else:
            if current_spindle > 0:
                row = get_tool(_conn, current_spindle)
                if row is not None:
                    home_pocket = row["tool_id"]
                    _conn.execute(
                        "UPDATE tool SET pocket = ? WHERE tool_id = ?",
                        (home_pocket, current_spindle),
                    )
                    _conn.commit()

        set_spindle_state(_conn, 0)


def _tolerant_loop() -> None:
    """Protocol loop that ignores stray blank lines on stdin.

    LinuxCNC's ``tooldata_db_notify`` at
    ``src/emc/tooldata/tooldata_db.cc:322`` writes ``"l %s\\n"`` where
    ``%s`` already ends in ``\\n``, so every ``l``/``u`` command arrives
    as two lines: the command and a stray blank. Silently skipping the
    blank keeps the reply stream aligned; NAK'ing it leaks ``!!!! NAK
    empty_line <>`` into the task log and desyncs subsequent reads.
    """
    tooldb.startup_ack()
    while True:
        try:
            raw = sys.stdin.readline()
            if raw == "":
                return
            line = raw.strip()
            if not line:
                continue
            tooldb.saveline(line)
            tooldb.do_cmd(line)
        except BrokenPipeError:
            sys.stdout.close()
            return
        except Exception as e:
            tooldb.nak_reply(f"_exception={e}")


def main(argv: list[str] | None = None) -> None:
    global _conn, _random_toolchanger

    if argv is None:
        argv = sys.argv[1:]

    db_path = argv[0] if argv else "tools.db"

    random_env = os.environ.get("RANDOM_TOOLCHANGER", "0")
    _random_toolchanger = random_env == "1"

    _log(f"opening database: {db_path}")
    _log(f"random_toolchanger: {_random_toolchanger}")

    _conn = open_db(db_path)

    ids = tool_ids(_conn)
    if not ids:
        _log("empty database — no tools registered")
    else:
        _log(f"loaded {len(ids)} tools: {ids}")

    tooldb_tools(ids)
    tooldb_callbacks(_user_get_tool, _user_put_tool,
                     _user_load_spindle, _user_unload_spindle)
    try:
        _tolerant_loop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
