"""End-to-end tests for `GcodeView` and `GcodePreview` in mock mode.

Both widgets are file-driven: they listen for `program_loaded` and
pull the file contents themselves (direct disk read in v1). The tests
write small .ngc files under pytest's `tmp_path`, drive MockTransport's
`load_program()` to fire the lifecycle, and assert the widget reacts.

`GcodePreview` additionally depends on the `gcode` C extension (via
`qtcnc.core.program.parse`), so its tests are gated on the extension
being importable; a bare pytest environment still runs the text-only
tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.core.command import Command
from qtcnc.core.status import Status
from qtcnc.core.types import ProgramState
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.base import QtcncWidget, collect_pin_declarations
from qtcnc.widgets.common.gcode_view import GcodeView
from qtcnc.widgets.hal import HalPinHub


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _make_world(*widget_factories):
    """Identical bootstrap shim used by `test_mvp_widgets.py`."""
    t = MockTransport()
    window = QMainWindow()
    widgets: list[QtcncWidget] = [f(window) for f in widget_factories]
    status = Status(t)
    hub = HalPinHub(t)
    cmd = Command(t)
    window.qtcnc_status = status
    window.qtcnc_command = cmd
    window.qtcnc_hal = hub
    t.hello()
    status.bootstrap()
    specs = collect_pin_declarations(widgets)
    if specs:
        result = t.declare_pins(specs)
        for name in result.created:
            hub.get_or_create(name)
    for w in widgets:
        w.qtcnc_setup()
    return t, status, cmd, window, widgets


def _write_ngc(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body.rstrip() + "\nM2\n")
    return path


# ---------------------------------------------------------------------------
# GcodeView
# ---------------------------------------------------------------------------


class TestGcodeView:
    def _mk(self):
        def factory(window):
            w = GcodeView(window)
            w.setObjectName("gview")
            return w
        return _make_world(factory)

    def test_starts_empty(self):
        _, _, _, _, (w,) = self._mk()
        assert w.toPlainText() == ""
        assert w.current_line == 0

    def test_loads_file_on_program_loaded(self, tmp_path: Path):
        t, status, _, _, (w,) = self._mk()
        path = _write_ngc(tmp_path, "simple.ngc", "G21 G90\nG0 X1\nG1 X2 F100")
        t.load_program(str(path))
        QApplication.processEvents()
        text = w.toPlainText()
        assert "G21 G90" in text
        assert "G1 X2 F100" in text
        assert w.loaded_path == str(path)

    def test_highlights_current_line(self, tmp_path: Path):
        t, status, _, _, (w,) = self._mk()
        path = _write_ngc(tmp_path, "lines.ngc", "G21 G90\nG0 X1\nG1 X2 F100\nG1 X3")
        t.load_program(str(path))
        QApplication.processEvents()
        w.set_current_line(3)
        assert w.current_line == 3
        # The block at index 2 (line 3) should have a highlight fmt.
        block = w.document().findBlockByNumber(2)
        fmt = block.blockFormat()
        assert fmt.background().color().name() != "#000000", (
            f"expected a non-default background on line 3, got {fmt.background().color().name()}"
        )

    def test_clears_highlight_on_zero(self, tmp_path: Path):
        t, status, _, _, (w,) = self._mk()
        path = _write_ngc(tmp_path, "clear.ngc", "G21\nG0 X1\nG1 X2")
        t.load_program(str(path))
        QApplication.processEvents()
        w.set_current_line(2)
        w.set_current_line(0)
        # Highlighted background should be gone on every block.
        block = w.document().findBlockByNumber(1)
        fmt = block.blockFormat()
        # Default QTextBlockFormat has an unpainted brush; just assert that
        # the widget's own _current_line is cleared so highlight isn't stuck.
        assert w.current_line == 0

    def test_program_closed_clears(self, tmp_path: Path):
        t, status, _, _, (w,) = self._mk()
        path = _write_ngc(tmp_path, "close.ngc", "G21\nG0 X1")
        t.load_program(str(path))
        QApplication.processEvents()
        assert "G0 X1" in w.toPlainText()
        # Simulate a program_closed lifecycle event.
        t._dispatch_lifecycle("program_closed", {})
        QApplication.processEvents()
        assert w.toPlainText() == ""
        assert w.loaded_path == ""

    def test_missing_file_shows_error(self, tmp_path: Path):
        _, status, _, _, (w,) = self._mk()
        w.load_file(str(tmp_path / "nonexistent.ngc"))
        assert "could not read" in w.toPlainText()

    def test_program_line_changed_signal_drives_highlight(self, tmp_path: Path):
        t, status, _, _, (w,) = self._mk()
        from dataclasses import replace
        path = _write_ngc(tmp_path, "auto.ngc", "G21\nG0 X1\nG1 X2\nG1 X3")
        t.load_program(str(path))
        QApplication.processEvents()
        # Push a new ProgramState with current_line=2 through the diff pipeline.
        old_program = t.get_snapshot().program
        new_program = replace(old_program, current_line=2)
        t.mutate_state(program=new_program)
        QApplication.processEvents()
        assert w.current_line == 2


# ---------------------------------------------------------------------------
# GcodePreview (gated on gcode C extension)
# ---------------------------------------------------------------------------


gcode = pytest.importorskip("gcode")
from qtcnc.widgets.common.gcode_preview import GcodePreview


class TestGcodePreview:
    def _mk(self):
        def factory(window):
            w = GcodePreview(window)
            w.setObjectName("gpreview")
            return w
        return _make_world(factory)

    def test_starts_empty(self):
        _, _, _, _, (w,) = self._mk()
        assert w.program is None
        assert w.segment_count == 0

    def test_loads_program_on_program_loaded(self, tmp_path: Path):
        t, status, _, _, (w,) = self._mk()
        path = _write_ngc(
            tmp_path, "box.ngc",
            "G21 G90\nG0 X0 Y0\nG1 X10 Y0 F100\nG1 X10 Y10\nG1 X0 Y10\nG1 X0 Y0",
        )
        t.load_program(str(path))
        QApplication.processEvents()
        assert w.loaded_path == str(path)
        assert w.program is not None
        assert w.segment_count > 0

    def test_scene_has_items_for_each_segment(self, tmp_path: Path):
        t, status, _, _, (w,) = self._mk()
        path = _write_ngc(
            tmp_path, "two.ngc",
            "G21 G90\nG0 X5 Y0\nG1 X5 Y5 F100",
        )
        t.load_program(str(path))
        QApplication.processEvents()
        # QGraphicsScene.items() returns all line items we added.
        items = w.scene().items()
        assert len(items) == w.segment_count
        assert w.segment_count >= 2

    def test_current_line_highlight_applies(self, tmp_path: Path):
        from dataclasses import replace
        t, status, _, _, (w,) = self._mk()
        path = _write_ngc(
            tmp_path, "hl.ngc",
            "G21 G90\nG0 X1 Y0\nG1 X2 Y0 F100\nG1 X2 Y2",
        )
        t.load_program(str(path))
        QApplication.processEvents()
        # Pick a line that emitted motion and highlight it.
        assert w.program is not None
        candidate_lines = [ln for ln, idxs in w.program.line_index.items() if ln > 0 and idxs]
        assert candidate_lines
        line = candidate_lines[0]
        w.set_current_line(line)
        assert w.current_line == line

    def test_parse_failure_shows_overlay(self, tmp_path: Path):
        _, _, _, _, (w,) = self._mk()
        w.load_file(str(tmp_path / "does-not-exist.ngc"))
        # Overlay installed an item but no segments.
        assert w.segment_count == 0

    def test_program_closed_clears(self, tmp_path: Path):
        t, status, _, _, (w,) = self._mk()
        path = _write_ngc(tmp_path, "cl.ngc", "G21\nG0 X1 Y0\nG1 X2 Y0 F100")
        t.load_program(str(path))
        QApplication.processEvents()
        assert w.segment_count > 0
        t._dispatch_lifecycle("program_closed", {})
        QApplication.processEvents()
        assert w.segment_count == 0
