"""sqlite schema and helpers for the qtcnc tool database.

Pure stdlib + sqlite3 — no Qt, no linuxcnc imports. Shared by the
DB_PROGRAM process and the daemon so they both speak the same schema.
WAL journal mode is set on first open so concurrent readers (daemon
polling) never block the writer (DB_PROGRAM handling M6, or daemon
handling ADD_TOOL from a remote client).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

_SCHEMA_VERSION = 1

_CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS tool (
    tool_id      INTEGER PRIMARY KEY,
    pocket       INTEGER NOT NULL,
    x_offset     REAL NOT NULL DEFAULT 0.0,
    y_offset     REAL NOT NULL DEFAULT 0.0,
    z_offset     REAL NOT NULL DEFAULT 0.0,
    a_offset     REAL NOT NULL DEFAULT 0.0,
    b_offset     REAL NOT NULL DEFAULT 0.0,
    c_offset     REAL NOT NULL DEFAULT 0.0,
    u_offset     REAL NOT NULL DEFAULT 0.0,
    v_offset     REAL NOT NULL DEFAULT 0.0,
    w_offset     REAL NOT NULL DEFAULT 0.0,
    diameter     REAL NOT NULL DEFAULT 0.0,
    frontangle   REAL NOT NULL DEFAULT 0.0,
    backangle    REAL NOT NULL DEFAULT 0.0,
    orientation  INTEGER NOT NULL DEFAULT 0,
    comment      TEXT NOT NULL DEFAULT '',
    UNIQUE(pocket)
);

CREATE TABLE IF NOT EXISTS spindle_state (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    tool_id   INTEGER NOT NULL DEFAULT 0,
    loaded_at TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO spindle_state (id, tool_id) VALUES (1, 0);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
"""

_OFFSET_LETTERS = ("x", "y", "z", "a", "b", "c", "u", "v", "w")

_TOOL_COLUMNS = (
    "tool_id", "pocket",
    "x_offset", "y_offset", "z_offset",
    "a_offset", "b_offset", "c_offset",
    "u_offset", "v_offset", "w_offset",
    "diameter", "frontangle", "backangle", "orientation",
    "comment",
)

_TOOLLINE_LETTER_MAP = {
    "T": "tool_id", "P": "pocket", "D": "diameter",
    "X": "x_offset", "Y": "y_offset", "Z": "z_offset",
    "A": "a_offset", "B": "b_offset", "C": "c_offset",
    "U": "u_offset", "V": "v_offset", "W": "w_offset",
    "I": "frontangle", "J": "backangle", "Q": "orientation",
}

_COLUMN_TO_LETTER = {v: k for k, v in _TOOLLINE_LETTER_MAP.items()}


def open_db(path: str, *, wal: bool = True) -> sqlite3.Connection:
    """Open (or create) the tool database and ensure the schema exists."""
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if wal:
        conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_CREATE_TABLES)
    return conn


def get_all_tools(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every tool row as a list of dicts, ordered by tool_id."""
    rows = conn.execute(
        "SELECT * FROM tool ORDER BY tool_id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_tool(conn: sqlite3.Connection, tool_id: int) -> dict[str, Any] | None:
    """Return a single tool row as a dict, or None if absent."""
    row = conn.execute(
        "SELECT * FROM tool WHERE tool_id = ?", (tool_id,)
    ).fetchone()
    return dict(row) if row else None


def upsert_tool(conn: sqlite3.Connection, tool_id: int, **fields: Any) -> None:
    """Insert or update a tool row.

    Keyword arguments map to column names (pocket, x_offset, diameter,
    comment, ...). Unknown keys are silently ignored.
    """
    existing = get_tool(conn, tool_id)
    valid = {k: v for k, v in fields.items() if k in _TOOL_COLUMNS and k != "tool_id"}

    if existing is None:
        if "pocket" not in valid:
            raise ValueError("pocket is required when inserting a new tool")
        cols = ["tool_id"] + list(valid.keys())
        placeholders = ", ".join("?" for _ in cols)
        vals = [tool_id] + list(valid.values())
        conn.execute(
            f"INSERT INTO tool ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
    else:
        if not valid:
            return
        set_clause = ", ".join(f"{k} = ?" for k in valid)
        conn.execute(
            f"UPDATE tool SET {set_clause} WHERE tool_id = ?",
            list(valid.values()) + [tool_id],
        )
    conn.commit()


def delete_tool(conn: sqlite3.Connection, tool_id: int) -> None:
    """Remove a tool. No-op if the tool does not exist."""
    conn.execute("DELETE FROM tool WHERE tool_id = ?", (tool_id,))
    conn.commit()


def tool_ids(conn: sqlite3.Connection) -> list[int]:
    """Return a sorted list of all tool_id values."""
    rows = conn.execute("SELECT tool_id FROM tool ORDER BY tool_id").fetchall()
    return [r[0] for r in rows]


def get_spindle_state(conn: sqlite3.Connection) -> int:
    """Return the tool_id currently in the spindle (0 = empty)."""
    row = conn.execute("SELECT tool_id FROM spindle_state WHERE id = 1").fetchone()
    return int(row[0]) if row else 0


def set_spindle_state(conn: sqlite3.Connection, tool_id: int) -> None:
    """Record which tool is in the spindle (0 = empty)."""
    conn.execute(
        "UPDATE spindle_state SET tool_id = ?, loaded_at = datetime('now') WHERE id = 1",
        (tool_id,),
    )
    conn.commit()


def next_available_pocket(conn: sqlite3.Connection) -> int:
    """Find the smallest positive integer not used as a pocket."""
    used = {r[0] for r in conn.execute("SELECT pocket FROM tool").fetchall()}
    pocket = 1
    while pocket in used:
        pocket += 1
    return pocket


def format_toolline(row: dict[str, Any]) -> str:
    """Convert a tool dict to the DB_PROGRAM text format.

    Example: ``T5 P5 Z1.500000 D6.000000 ;endmill``
    """
    parts: list[str] = []
    for col, letter in _COLUMN_TO_LETTER.items():
        val = row.get(col)
        if val is None:
            continue
        if col == "orientation":
            parts.append(f"{letter}{int(val)}")
        elif col in ("tool_id", "pocket"):
            parts.append(f"{letter}{int(val)}")
        else:
            parts.append(f"{letter}{float(val):.6f}")
    comment = row.get("comment", "")
    if comment:
        parts.append(f";{comment}")
    return " ".join(parts)


def parse_toolline(line: str) -> dict[str, Any]:
    """Parse a DB_PROGRAM-format tool line into a dict.

    Example input: ``T5 P5 Z1.5 D6.0 ;endmill``
    """
    result: dict[str, Any] = {}
    comment = ""
    if ";" in line:
        line, comment = line.split(";", 1)
        comment = comment.strip()

    for token in line.upper().split():
        if not token:
            continue
        letter = token[0]
        if letter not in _TOOLLINE_LETTER_MAP:
            continue
        col = _TOOLLINE_LETTER_MAP[letter]
        raw = token[1:]
        if col in ("tool_id", "pocket", "orientation"):
            result[col] = int(float(raw))
        else:
            result[col] = float(raw)

    if comment:
        result["comment"] = comment
    return result


def swap_pockets(
    conn: sqlite3.Connection, tool_a_id: int, pocket_a: int,
    tool_b_id: int, pocket_b: int,
) -> None:
    """Swap two tools' pockets atomically (for random toolchangers).

    Either tool_id may be 0, meaning "no tool in that pocket" — in that
    case only the non-zero tool's pocket is updated.
    """
    sentinel = -999999
    if tool_a_id > 0:
        conn.execute(
            "UPDATE tool SET pocket = ? WHERE tool_id = ?",
            (sentinel, tool_a_id),
        )
    if tool_b_id > 0:
        conn.execute(
            "UPDATE tool SET pocket = ? WHERE tool_id = ?",
            (pocket_a, tool_b_id),
        )
    if tool_a_id > 0:
        conn.execute(
            "UPDATE tool SET pocket = ? WHERE tool_id = ?",
            (pocket_b, tool_a_id),
        )
    conn.commit()
