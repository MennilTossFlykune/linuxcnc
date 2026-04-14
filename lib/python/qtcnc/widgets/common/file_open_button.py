"""FileOpenButton: opens a g-code file picker, dispatches load_program."""

from __future__ import annotations

import os
from typing import Any

from qtpy.QtWidgets import QFileDialog, QPushButton

from qtcnc.core.types import ProgramState
from qtcnc.widgets.base import QtcncWidget


_GCODE_FILTER = "G-code files (*.ngc *.nc *.tap *.gcode);;All files (*)"

_OK_STYLE = ""
_FAIL_STYLE = "QPushButton { background-color: #c33; color: white; }"


class FileOpenButton(QtcncWidget, QPushButton):
    """Push button that opens a `QFileDialog` and dispatches `load_program`.

    * Click → `QFileDialog.getOpenFileName` filtered to g-code extensions.
    * On `program_loaded`: clear any failure styling and show the basename.
    * On `program_load_failed`: red highlight, tooltip carries the reason.
    * On `program_missing`: red highlight, tooltip says the file is gone.
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._default_text: str = "Open G-code"
        self._start_dir: str = ""
        self.setText(self._default_text)
        self.clicked.connect(self._on_clicked)

    def qtcnc_setup(self) -> None:
        self.connect_status("program_loaded", self._on_loaded)
        self.connect_status("program_load_failed", self._on_load_failed)
        self.connect_status("program_missing", self._on_missing)
        # Seed from current snapshot in case a program is already open.
        program = self.window().qtcnc_status.state.program
        if program.path:
            self._on_loaded(program.path, program)

    def _on_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open G-code program",
            self._start_dir,
            _GCODE_FILTER,
        )
        if not path:
            return
        self._start_dir = os.path.dirname(path)
        cmd = self.window().qtcnc_command
        cmd.load_program(path)

    def _on_loaded(self, path: str, program: ProgramState) -> None:
        self.setStyleSheet(_OK_STYLE)
        self.setToolTip(path)
        self.setText(os.path.basename(path) or self._default_text)

    def _on_load_failed(self, path: str, reason: str) -> None:
        self.setStyleSheet(_FAIL_STYLE)
        self.setToolTip(f"load failed: {reason}")
        self.setText(os.path.basename(path) or self._default_text)

    def _on_missing(self, path: str) -> None:
        self.setStyleSheet(_FAIL_STYLE)
        self.setToolTip(f"file missing: {path}")
        self.setText(os.path.basename(path) or self._default_text)
