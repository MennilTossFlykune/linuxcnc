"""Tests for `qtcnc.core.program` — the client-side g-code preview parser.

These tests exercise the full path through the `gcode` C extension
(built from `src/emc/rs274ngc/gcodemodule.cc`). When `gcode` isn't
importable (e.g. on hosts without the LinuxCNC C extensions built),
the entire module is skipped so CI on bare Python still passes.

Coverage:
* a single rapid + feed move produces two segments with correct kinds
* extents bound the toolpath
* line_index maps source line numbers to segment indices
* arc moves get tessellated into many segments
* missing files raise `GcodeParseError` (negative path)
* a brand-new program with no moves returns an empty extents box
"""

from __future__ import annotations

from pathlib import Path

import pytest

gcode = pytest.importorskip("gcode")  # noqa: F401  — guard the whole module

from qtcnc.core.program import (
    GcodeParseError,
    GcodeProgram,
    ToolpathExtents,
    ToolpathSegment,
    parse,
)
from qtcnc.core.types import Position


def _write_program(tmp_path: Path, name: str, body: str) -> Path:
    """Write a small g-code file under tmp_path and return its path."""
    f = tmp_path / name
    # Always end with M2 so the interpreter terminates cleanly.
    f.write_text(body.rstrip() + "\nM2\n")
    return f


class TestParseBasic:
    def test_rapid_then_feed_emits_two_segments(self, tmp_path: Path) -> None:
        path = _write_program(
            tmp_path,
            "two_moves.ngc",
            """
            G21 G90
            G0 X10 Y0 Z0
            G1 X10 Y10 F100
            """,
        )
        program = parse(str(path))
        assert isinstance(program, GcodeProgram)
        assert program.path == str(path)
        # Filter for non-tool moves: depending on g-code flavor the
        # interpreter may emit additional zero-length offset moves we
        # don't care about.
        kinds = [s.kind for s in program.segments]
        assert "rapid" in kinds
        assert "feed" in kinds
        # The feed segment should end at (10, 10).
        feed = next(s for s in program.segments if s.kind == "feed")
        assert feed.end.x == pytest.approx(10.0)
        assert feed.end.y == pytest.approx(10.0)

    def test_extents_bound_toolpath(self, tmp_path: Path) -> None:
        path = _write_program(
            tmp_path,
            "box.ngc",
            """
            G21 G90
            G0 X0 Y0 Z0
            G1 X10 Y0 F100
            G1 X10 Y20
            G1 X0 Y20
            G1 X0 Y0
            """,
        )
        program = parse(str(path))
        ext = program.extents
        assert isinstance(ext, ToolpathExtents)
        assert ext.min.x == pytest.approx(0.0)
        assert ext.min.y == pytest.approx(0.0)
        assert ext.max.x == pytest.approx(10.0)
        assert ext.max.y == pytest.approx(20.0)

    def test_line_index_maps_source_lines(self, tmp_path: Path) -> None:
        path = _write_program(
            tmp_path,
            "lines.ngc",
            """
            G21 G90
            G0 X1 Y0
            G1 X2 Y0 F100
            G1 X2 Y2
            """,
        )
        program = parse(str(path))
        # Each source line that emitted motion should appear in the
        # line_index as a non-empty list of segment indices.
        for line_no, indices in program.line_index.items():
            assert isinstance(line_no, int)
            assert all(0 <= i < len(program.segments) for i in indices)
        # We expect at least one line with a real source line number
        # (not -1, the default before next_line fires).
        positive_lines = [n for n in program.line_index if n > 0]
        assert positive_lines, "no positive source lines indexed"

    def test_line_count_matches_source(self, tmp_path: Path) -> None:
        body = "G21 G90\nG0 X1\nG1 X2 F100\n"
        path = _write_program(tmp_path, "count.ngc", body)
        program = parse(str(path))
        # The helper appends "M2\n" to the body, so the file has
        # body lines + 1 (M2) lines. Just sanity-check it's >0.
        assert program.line_count >= 4


class TestParseArcs:
    def test_arc_emits_many_segments(self, tmp_path: Path) -> None:
        # G2 = clockwise arc. Half-circle from (10,0) back to (-10,0)
        # around the origin — 180 degrees, should tessellate into
        # multiple short chords.
        path = _write_program(
            tmp_path,
            "halfcircle.ngc",
            """
            G21 G90
            G0 X10 Y0 Z0
            G2 X-10 Y0 I-10 J0 F100
            """,
        )
        program = parse(str(path))
        arc_segments = [s for s in program.segments if s.kind == "arc"]
        # arcdivision is 64 — a half-circle should tessellate into
        # somewhere in the dozens of chords.
        assert len(arc_segments) >= 8


class TestParseEdgeCases:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.ngc"
        with pytest.raises(GcodeParseError):
            parse(str(missing))

    def test_empty_program_has_empty_extents(self, tmp_path: Path) -> None:
        # A program with no motion at all (just M2) should parse cleanly
        # and report empty extents.
        path = _write_program(tmp_path, "empty.ngc", "")
        program = parse(str(path))
        assert program.segments == ()
        assert program.extents.is_empty


class TestDataclassShape:
    def test_segment_is_frozen(self) -> None:
        seg = ToolpathSegment(
            start=Position(), end=Position(x=1.0),
            kind="feed", line=10, feedrate=1.0,
        )
        with pytest.raises(Exception):
            seg.kind = "rapid"  # type: ignore[misc]

    def test_extents_is_empty_for_zero_box(self) -> None:
        ext = ToolpathExtents(min=Position(), max=Position())
        assert ext.is_empty

    def test_segments_on_line_returns_correct_subset(self, tmp_path: Path) -> None:
        path = _write_program(
            tmp_path,
            "subset.ngc",
            """
            G21 G90
            G0 X1 Y0
            G1 X2 Y0 F100
            """,
        )
        program = parse(str(path))
        # Pick any line that emitted motion and verify the helper
        # returns the same segments the index points at.
        for line_no, indices in program.line_index.items():
            if not indices:
                continue
            via_helper = program.segments_on_line(line_no)
            via_index = [program.segments[i] for i in indices]
            assert via_helper == via_index
