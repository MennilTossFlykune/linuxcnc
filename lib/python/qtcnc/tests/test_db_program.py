"""Tests for qtcnc.tools.db_program — DB_PROGRAM callback logic.

These tests exercise the callback functions directly against an
in-memory sqlite database, bypassing the tooldb stdin/stdout loop.
"""

from __future__ import annotations

import io

import pytest

import tooldb

from qtcnc.tools import db_program
from qtcnc.tools.tooldb_schema import (
    get_spindle_state,
    get_tool,
    open_db,
    tool_ids,
    upsert_tool,
)


@pytest.fixture()
def nonrandom_db(monkeypatch):
    conn = open_db(":memory:", wal=False)
    upsert_tool(conn, 1, pocket=1, z_offset=-25.4, diameter=6.0, comment="6mm endmill")
    upsert_tool(conn, 2, pocket=2, z_offset=-30.0, diameter=10.0)
    upsert_tool(conn, 5, pocket=5, x_offset=0.1, z_offset=-20.0, diameter=3.0)
    monkeypatch.setattr(db_program, "_conn", conn)
    monkeypatch.setattr(db_program, "_random_toolchanger", False)
    yield conn
    conn.close()


@pytest.fixture()
def random_db(monkeypatch):
    conn = open_db(":memory:", wal=False)
    upsert_tool(conn, 1, pocket=101, z_offset=-25.4, diameter=6.0)
    upsert_tool(conn, 2, pocket=102, z_offset=-30.0, diameter=10.0)
    upsert_tool(conn, 5, pocket=105, x_offset=0.1, z_offset=-20.0, diameter=3.0)
    monkeypatch.setattr(db_program, "_conn", conn)
    monkeypatch.setattr(db_program, "_random_toolchanger", True)
    yield conn
    conn.close()


class TestGetTool:
    def test_existing_tool(self, nonrandom_db):
        line = db_program._user_get_tool(1)
        assert "T1" in line
        assert "P1" in line

    def test_missing_tool_raises(self, nonrandom_db):
        with pytest.raises(KeyError):
            db_program._user_get_tool(999)


class TestPutTool:
    def test_update_offset(self, nonrandom_db):
        db_program._user_put_tool(1, "T1 P1 Z-50.0 D6.0")
        row = get_tool(nonrandom_db, 1)
        assert row["z_offset"] == pytest.approx(-50.0)

    def test_preserves_other_fields(self, nonrandom_db):
        db_program._user_put_tool(1, "T1 P1 D8.0")
        row = get_tool(nonrandom_db, 1)
        assert row["diameter"] == pytest.approx(8.0)
        assert row["z_offset"] == pytest.approx(-25.4)


class TestNonRandomLoadUnload:
    def test_load_moves_to_pocket_zero(self, nonrandom_db):
        db_program._user_load_spindle(1, "T1 P0")
        row = get_tool(nonrandom_db, 1)
        assert row["pocket"] == 0
        assert get_spindle_state(nonrandom_db) == 1

    def test_unload_restores_home_pocket(self, nonrandom_db):
        db_program._user_load_spindle(1, "T1 P0")
        db_program._user_unload_spindle(0, "T0 P0")
        row = get_tool(nonrandom_db, 1)
        assert row["pocket"] == 1
        assert get_spindle_state(nonrandom_db) == 0

    def test_load_then_load_different(self, nonrandom_db):
        db_program._user_load_spindle(1, "T1 P0")
        db_program._user_unload_spindle(0, "T0 P0")
        db_program._user_load_spindle(2, "T2 P0")
        assert get_tool(nonrandom_db, 1)["pocket"] == 1
        assert get_tool(nonrandom_db, 2)["pocket"] == 0
        assert get_spindle_state(nonrandom_db) == 2

    def test_load_evicts_existing_pocket_zero_occupant(self, nonrandom_db):
        db_program._user_load_spindle(1, "T1 P0")
        db_program._user_load_spindle(2, "T2 P0")
        assert get_tool(nonrandom_db, 1)["pocket"] == 1
        assert get_tool(nonrandom_db, 2)["pocket"] == 0
        assert get_spindle_state(nonrandom_db) == 2


class TestRandomLoadUnload:
    def test_load_moves_to_pocket_zero(self, random_db):
        db_program._user_load_spindle(1, "T1 P0")
        assert get_tool(random_db, 1)["pocket"] == 0
        assert get_spindle_state(random_db) == 1

    def test_unload_assigns_destination_pocket(self, random_db):
        db_program._user_load_spindle(1, "T1 P0")
        db_program._user_unload_spindle(1, "T1 P102")
        assert get_tool(random_db, 1)["pocket"] == 102
        assert get_spindle_state(random_db) == 0

    def test_paired_tool_change(self, random_db):
        """Random toolchanger: unload old to new's pocket, load new to spindle."""
        db_program._user_load_spindle(1, "T1 P0")
        assert get_spindle_state(random_db) == 1

        db_program._user_unload_spindle(1, "T1 P102")
        db_program._user_load_spindle(2, "T2 P0")

        assert get_tool(random_db, 1)["pocket"] == 102
        assert get_tool(random_db, 2)["pocket"] == 0
        assert get_spindle_state(random_db) == 2


class TestTolerantLoop:
    """LinuxCNC's tooldata_db_notify at tooldata_db.cc:322 appends \\n to
    a buffer that already ends in \\n. Each l/u command arrives as a
    valid line followed by a stray blank. The loop must consume the
    blank silently so the reply stream stays aligned with the request
    stream.
    """

    def _run(self, nonrandom_db, stdin_text, monkeypatch):
        stdin = io.StringIO(stdin_text)
        stdout = io.StringIO()
        monkeypatch.setattr(db_program.sys, "stdin", stdin)
        monkeypatch.setattr(db_program.sys, "stdout", stdout)
        monkeypatch.setattr(tooldb.sys, "stdin", stdin)
        monkeypatch.setattr(tooldb.sys, "stdout", stdout)
        tooldb.tooldb_tools(tool_ids(nonrandom_db))
        tooldb.tooldb_callbacks(
            db_program._user_get_tool,
            db_program._user_put_tool,
            db_program._user_load_spindle,
            db_program._user_unload_spindle,
        )
        db_program._tolerant_loop()
        return stdout.getvalue()

    def test_double_newline_load_emits_no_nak(self, nonrandom_db, monkeypatch):
        out = self._run(nonrandom_db, "l T1 P0 \n\n", monkeypatch)
        assert "NAK" not in out
        assert "FINI" in out

    def test_two_loads_with_trailing_blanks(self, nonrandom_db, monkeypatch):
        """Exact reproducer for the M6T1 then M6T2 bug."""
        out = self._run(nonrandom_db, "l T1 P0 \n\nl T2 P0 \n\n", monkeypatch)
        assert "NAK" not in out
        assert out.count("FINI (update recvd)") == 2
        assert get_tool(nonrandom_db, 2)["pocket"] == 0
        assert get_spindle_state(nonrandom_db) == 2

    def test_eof_exits_cleanly(self, nonrandom_db, monkeypatch):
        out = self._run(nonrandom_db, "", monkeypatch)
        assert "v2.1" in out
