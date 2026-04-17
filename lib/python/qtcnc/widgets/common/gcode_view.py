"""GcodeView: read-only text view of the currently loaded g-code program.

Subscribes to `program_loaded` and shows the file contents in a
`QPlainTextEdit`; subscribes to `motion_line_changed` and highlights
the line being executed with a background colour.

File access: the widget reads the file directly from disk via
`_load_file_bytes`, a single seam that swaps to a `GET_FILE(path)` REQ
round-trip once remote operation lands. The disk read has a 5 MB cap
so a giant program can't freeze the GUI thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from qtpy.QtCore import Qt
from qtpy.QtGui import QColor, QTextBlockFormat, QTextCursor
from qtpy.QtWidgets import QPlainTextEdit

from qtcnc.core.types import ProgramState
from qtcnc.widgets.base import QtcncWidget


_MAX_BYTES = 5 * 1024 * 1024  # 5 MB cap (matches A5 plan for GET_FILE)
_HIGHLIGHT_COLOR = QColor(255, 255, 160)   # pale yellow — visible on any theme


class GcodeView(QtcncWidget, QPlainTextEdit):
    """Read-only g-code display with current-line highlighting.

    The widget is content-agnostic beyond text: pass it a file path
    via `load_file(path)` (normally driven by `program_loaded`) and it
    shows the contents. Current-line highlight is updated from
    `program_line_changed`, but widget code can also call
    `set_current_line(n)` directly from tests or a handler.
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._current_line: int = 0
        self._loaded_path: str = ""

    # ----- lifecycle -----

    def qtcnc_setup(self) -> None:
        self.connect_status("program_loaded", self._on_program_loaded)
        self.connect_status("motion_line_changed", self._on_line_changed)
        self.connect_status("program_closed", self._on_program_closed)
        state = self.window().qtcnc_status.state
        if state.program.path:
            self._on_program_loaded(state.program.path, state.program)
            if state.program.motion_line > 0:
                self.set_current_line(state.program.motion_line)

    # ----- public API -----

    def load_file(self, path: str) -> None:
        """Load `path` into the view. Empty path clears the display."""
        if not path:
            self.clear()
            self._loaded_path = ""
            return
        try:
            contents = self._load_file_bytes(path)
        except OSError as e:
            self.setPlainText(f"[qtcnc] could not read {path}: {e}")
            self._loaded_path = ""
            return
        try:
            text = contents.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            text = contents.decode("latin-1", errors="replace")
        self.setPlainText(text)
        self._loaded_path = path
        self._current_line = 0

    def set_current_line(self, line: int) -> None:
        """Highlight source line `line` (1-indexed). 0 clears highlight."""
        self._current_line = int(line)
        self._apply_highlight()

    @property
    def current_line(self) -> int:
        return self._current_line

    @property
    def loaded_path(self) -> str:
        return self._loaded_path

    # ----- internals -----

    def _on_program_loaded(self, path: str, _program: ProgramState) -> None:
        self.load_file(path)

    def _on_program_closed(self) -> None:
        self.load_file("")

    def _on_line_changed(self, line: int) -> None:
        self.set_current_line(line)

    def _load_file_bytes(self, path: str) -> bytes:
        """Read a g-code file from disk, capped at _MAX_BYTES.

        Single seam for a future remote-mode switch: when running
        against a daemon on another host, this will issue a GET_FILE
        REQ instead of a direct filesystem read.
        """
        p = Path(path)
        size = p.stat().st_size
        if size > _MAX_BYTES:
            raise OSError(
                f"program is {size} bytes; GcodeView caps at {_MAX_BYTES}"
            )
        with open(p, "rb") as f:
            return f.read(_MAX_BYTES)

    def _apply_highlight(self) -> None:
        doc = self.document()
        line = self._current_line
        if line <= 0 or doc is None:
            self._clear_highlight()
            return
        # Clear previous highlight, then mark the new line.
        self._clear_highlight()
        block = doc.findBlockByNumber(line - 1)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        fmt = QTextBlockFormat()
        fmt.setBackground(_HIGHLIGHT_COLOR)
        cursor.setBlockFormat(fmt)
        self.setTextCursor(cursor)
        self.centerCursor()

    def _clear_highlight(self) -> None:
        doc = self.document()
        if doc is None:
            return
        cursor = QTextCursor(doc)
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        cursor.setBlockFormat(fmt)
