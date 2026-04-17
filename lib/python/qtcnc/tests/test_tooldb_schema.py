"""Tests for qtcnc.tools.tooldb_schema — sqlite schema and helpers."""

from __future__ import annotations

import sqlite3

import pytest

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


@pytest.fixture()
def db():
    conn = open_db(":memory:", wal=False)
    yield conn
    conn.close()


@pytest.fixture()
def seeded_db(db):
    upsert_tool(db, 1, pocket=1, z_offset=-25.4, diameter=6.0, comment="6mm endmill")
    upsert_tool(db, 2, pocket=2, z_offset=-30.0, diameter=10.0, comment="10mm endmill")
    upsert_tool(db, 5, pocket=5, x_offset=0.1, z_offset=-20.0, diameter=3.0)
    return db


class TestOpenDb:
    def test_creates_tables(self, db):
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "tool" in tables
        assert "spindle_state" in tables
        assert "schema_version" in tables

    def test_schema_version(self, db):
        row = db.execute("SELECT version FROM schema_version").fetchone()
        assert row[0] == 1

    def test_spindle_state_seeded(self, db):
        assert get_spindle_state(db) == 0

    def test_idempotent(self, db):
        open_db(":memory:", wal=False)


class TestUpsertAndGet:
    def test_insert(self, db):
        upsert_tool(db, 1, pocket=1, z_offset=-25.4, diameter=6.0)
        row = get_tool(db, 1)
        assert row is not None
        assert row["tool_id"] == 1
        assert row["pocket"] == 1
        assert row["z_offset"] == pytest.approx(-25.4)
        assert row["diameter"] == pytest.approx(6.0)
        assert row["x_offset"] == pytest.approx(0.0)

    def test_update(self, db):
        upsert_tool(db, 1, pocket=1, diameter=6.0)
        upsert_tool(db, 1, diameter=8.0)
        row = get_tool(db, 1)
        assert row["diameter"] == pytest.approx(8.0)
        assert row["pocket"] == 1

    def test_insert_requires_pocket(self, db):
        with pytest.raises(ValueError, match="pocket is required"):
            upsert_tool(db, 1, diameter=6.0)

    def test_unknown_keys_ignored(self, db):
        upsert_tool(db, 1, pocket=1, bogus=999)
        row = get_tool(db, 1)
        assert row is not None

    def test_get_nonexistent(self, db):
        assert get_tool(db, 999) is None

    def test_update_no_fields_is_noop(self, db):
        upsert_tool(db, 1, pocket=1, diameter=6.0)
        upsert_tool(db, 1)
        assert get_tool(db, 1)["diameter"] == pytest.approx(6.0)

    def test_comment(self, db):
        upsert_tool(db, 1, pocket=1, comment="test endmill")
        assert get_tool(db, 1)["comment"] == "test endmill"

    def test_all_offsets(self, db):
        upsert_tool(
            db, 1, pocket=1,
            x_offset=1.0, y_offset=2.0, z_offset=3.0,
            a_offset=4.0, b_offset=5.0, c_offset=6.0,
            u_offset=7.0, v_offset=8.0, w_offset=9.0,
        )
        row = get_tool(db, 1)
        for i, letter in enumerate("xyzabcuvw", start=1):
            assert row[f"{letter}_offset"] == pytest.approx(float(i))


class TestDelete:
    def test_delete_existing(self, seeded_db):
        delete_tool(seeded_db, 1)
        assert get_tool(seeded_db, 1) is None
        assert len(get_all_tools(seeded_db)) == 2

    def test_delete_nonexistent_is_noop(self, db):
        delete_tool(db, 999)


class TestGetAllTools:
    def test_empty(self, db):
        assert get_all_tools(db) == []

    def test_ordered_by_tool_id(self, seeded_db):
        tools = get_all_tools(seeded_db)
        assert [t["tool_id"] for t in tools] == [1, 2, 5]


class TestToolIds:
    def test_empty(self, db):
        assert tool_ids(db) == []

    def test_sorted(self, seeded_db):
        assert tool_ids(seeded_db) == [1, 2, 5]


class TestSpindleState:
    def test_default_empty(self, db):
        assert get_spindle_state(db) == 0

    def test_set_and_get(self, db):
        set_spindle_state(db, 5)
        assert get_spindle_state(db) == 5

    def test_clear(self, db):
        set_spindle_state(db, 5)
        set_spindle_state(db, 0)
        assert get_spindle_state(db) == 0


class TestNextAvailablePocket:
    def test_empty_db(self, db):
        assert next_available_pocket(db) == 1

    def test_gap(self, db):
        upsert_tool(db, 1, pocket=1)
        upsert_tool(db, 3, pocket=3)
        assert next_available_pocket(db) == 2

    def test_no_gap(self, seeded_db):
        assert next_available_pocket(seeded_db) == 3


class TestFormatToolline:
    def test_basic(self):
        row = {"tool_id": 5, "pocket": 5, "z_offset": 1.5, "diameter": 6.0}
        line = format_toolline(row)
        assert "T5" in line
        assert "P5" in line
        assert "Z1.500000" in line
        assert "D6.000000" in line

    def test_with_comment(self):
        row = {"tool_id": 1, "pocket": 1, "comment": "endmill"}
        line = format_toolline(row)
        assert line.endswith(";endmill")

    def test_no_comment(self):
        row = {"tool_id": 1, "pocket": 1}
        line = format_toolline(row)
        assert ";" not in line

    def test_orientation(self):
        row = {"tool_id": 1, "pocket": 1, "orientation": 3}
        line = format_toolline(row)
        assert "Q3" in line

    def test_zero_offsets_included(self):
        row = {"tool_id": 1, "pocket": 1, "x_offset": 0.0}
        line = format_toolline(row)
        assert "X0.000000" in line


class TestParseToolline:
    def test_basic(self):
        result = parse_toolline("T5 P5 Z1.5 D6.0")
        assert result["tool_id"] == 5
        assert result["pocket"] == 5
        assert result["z_offset"] == pytest.approx(1.5)
        assert result["diameter"] == pytest.approx(6.0)

    def test_with_comment(self):
        result = parse_toolline("T1 P1 Z-25.4 ;6mm endmill")
        assert result["tool_id"] == 1
        assert result["comment"] == "6mm endmill"
        assert result["z_offset"] == pytest.approx(-25.4)

    def test_all_letters(self):
        result = parse_toolline("T1 P1 X1.0 Y2.0 Z3.0 A4.0 B5.0 C6.0 U7.0 V8.0 W9.0 D10.0 I11.0 J12.0 Q3")
        assert result["x_offset"] == pytest.approx(1.0)
        assert result["y_offset"] == pytest.approx(2.0)
        assert result["z_offset"] == pytest.approx(3.0)
        assert result["a_offset"] == pytest.approx(4.0)
        assert result["b_offset"] == pytest.approx(5.0)
        assert result["c_offset"] == pytest.approx(6.0)
        assert result["u_offset"] == pytest.approx(7.0)
        assert result["v_offset"] == pytest.approx(8.0)
        assert result["w_offset"] == pytest.approx(9.0)
        assert result["diameter"] == pytest.approx(10.0)
        assert result["frontangle"] == pytest.approx(11.0)
        assert result["backangle"] == pytest.approx(12.0)
        assert result["orientation"] == 3

    def test_case_insensitive(self):
        result = parse_toolline("t5 p5 z1.5")
        assert result["tool_id"] == 5

    def test_round_trip(self, seeded_db):
        for row in get_all_tools(seeded_db):
            line = format_toolline(row)
            parsed = parse_toolline(line)
            assert parsed["tool_id"] == row["tool_id"]
            assert parsed["pocket"] == row["pocket"]
            assert parsed.get("z_offset", 0.0) == pytest.approx(row["z_offset"])
            assert parsed.get("diameter", 0.0) == pytest.approx(row["diameter"])


class TestSwapPockets:
    def test_swap_two_tools(self, db):
        upsert_tool(db, 10, pocket=10)
        upsert_tool(db, 20, pocket=20)
        swap_pockets(db, 10, 10, 20, 20)
        assert get_tool(db, 10)["pocket"] == 20
        assert get_tool(db, 20)["pocket"] == 10

    def test_swap_tool_with_empty(self, db):
        upsert_tool(db, 10, pocket=10)
        swap_pockets(db, 10, 10, 0, 0)
        assert get_tool(db, 10)["pocket"] == 0

    def test_swap_empty_with_tool(self, db):
        upsert_tool(db, 10, pocket=5)
        swap_pockets(db, 0, 0, 10, 5)
        assert get_tool(db, 10)["pocket"] == 0


class TestPocketUniqueness:
    def test_duplicate_pocket_raises(self, db):
        upsert_tool(db, 1, pocket=1)
        with pytest.raises(sqlite3.IntegrityError):
            upsert_tool(db, 2, pocket=1)
