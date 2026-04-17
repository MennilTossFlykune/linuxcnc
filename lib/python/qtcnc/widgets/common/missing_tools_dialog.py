"""MissingToolsDialog: prompt the operator to auto-add tools not in the DB.

Displayed when a g-code program is loaded and the preview parse reports
tool numbers that have no matching entry in the tool database. Each
missing tool gets a row with a checkbox (checked by default), the tool
number, and a pocket spinbox (defaulting to the tool number).

The handler calls ``dialog.exec()`` — if Accepted, ``selected_tools()``
returns the ``(tool_id, pocket)`` pairs the operator wants to create.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from qtcnc.widgets.common.dialog import QtcncDialog, Severity


class MissingToolsDialog(QtcncDialog):
    """Dialog listing missing tools with checkboxes and pocket spinboxes."""

    def __init__(
        self, missing: set[int] | frozenset[int], parent: Any = None,
    ) -> None:
        self._missing = sorted(missing)
        self._rows: list[tuple[QCheckBox, int, QSpinBox]] = []
        n = len(missing)
        super().__init__(
            parent,
            title="Missing Tools",
            message=(
                f"{n} tool(s) referenced in the program "
                "are not in the tool database:"
            ),
            severity=Severity.WARNING,
            blocking=True,
            mandatory=False,
            buttons=(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            ),
        )
        self.button(QDialogButtonBox.StandardButton.Ok).setText("Add Selected")
        self.button(QDialogButtonBox.StandardButton.Cancel).setText("Skip")

    def build_content(self, layout: QVBoxLayout) -> None:
        for tid in self._missing:
            row_layout = QHBoxLayout()
            cb = QCheckBox(f"T{tid}", self)
            cb.setChecked(True)
            pocket_spin = QSpinBox(self)
            pocket_spin.setRange(1, 99999)
            pocket_spin.setValue(tid)
            pocket_spin.setPrefix("P")
            row_layout.addWidget(cb)
            row_layout.addStretch()
            row_layout.addWidget(QLabel("Pocket:", self))
            row_layout.addWidget(pocket_spin)
            layout.addLayout(row_layout)
            self._rows.append((cb, tid, pocket_spin))

    def selected_tools(self) -> list[tuple[int, int]]:
        """Return (tool_id, pocket) for every checked row."""
        out: list[tuple[int, int]] = []
        for cb, tid, pocket_spin in self._rows:
            if cb.isChecked():
                out.append((tid, pocket_spin.value()))
        return out

    @property
    def rows(self) -> list[tuple[QCheckBox, int, QSpinBox]]:
        return list(self._rows)
