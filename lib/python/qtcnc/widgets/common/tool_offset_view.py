"""ToolOffsetView: editable table of tool offsets backed by the daemon's sqlite DB.

The widget never opens a database directly — all reads and writes go
through the Command facade (GET_TOOL_DB / ADD_TOOL / REMOVE_TOOL /
UPDATE_TOOL), so remote clients work identically to local ones.

Layout: a QTableView with a ToolOffsetModel, plus Add / Remove buttons
in a toolbar row above. Editing a cell dispatches UPDATE_TOOL through
the command facade. The widget refreshes when it receives a
tool_table_changed signal from Status (fires whenever the daemon's stat
poll sees a changed tool_table — including after MissingToolsDialog adds).
"""

from __future__ import annotations

from dataclasses import fields as dc_fields
from typing import Any

from qtpy.QtCore import QAbstractTableModel, QModelIndex, Qt
from qtpy.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from qtcnc.core.types import GetToolDbResult, ToolDbEntry
from qtcnc.widgets.base import QtcncWidget

_COLUMNS = [
    ("tool_id", "Tool #", False),
    ("pocket", "Pocket", True),
    ("x_offset", "X", True),
    ("y_offset", "Y", True),
    ("z_offset", "Z", True),
    ("a_offset", "A", True),
    ("b_offset", "B", True),
    ("c_offset", "C", True),
    ("u_offset", "U", True),
    ("v_offset", "V", True),
    ("w_offset", "W", True),
    ("diameter", "Dia", True),
    ("frontangle", "Front", True),
    ("backangle", "Back", True),
    ("orientation", "Orient", True),
    ("comment", "Comment", True),
]


class ToolOffsetModel(QAbstractTableModel):
    """Table model backed by a list of ToolDbEntry from GET_TOOL_DB."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._tools: list[ToolDbEntry] = []
        self._command: Any = None

    def set_command(self, command: Any) -> None:
        self._command = command

    def set_tools(self, tools: list[ToolDbEntry] | tuple[ToolDbEntry, ...]) -> None:
        self.beginResetModel()
        self._tools = list(tools)
        self.endResetModel()

    @property
    def tools(self) -> list[ToolDbEntry]:
        return list(self._tools)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._tools)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(_COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(_COLUMNS):
            return _COLUMNS[section][1]
        if orientation == Qt.Orientation.Vertical:
            return str(section + 1)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or role not in (
            Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole,
        ):
            return None
        row, col = index.row(), index.column()
        if row < 0 or row >= len(self._tools) or col < 0 or col >= len(_COLUMNS):
            return None
        field_name = _COLUMNS[col][0]
        value = getattr(self._tools[row], field_name)
        if role == Qt.ItemDataRole.EditRole:
            return value
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        if not index.isValid():
            return base
        col = index.column()
        if 0 <= col < len(_COLUMNS) and _COLUMNS[col][2]:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        row, col = index.row(), index.column()
        if row < 0 or row >= len(self._tools) or col < 0 or col >= len(_COLUMNS):
            return False
        field_name, _, editable = _COLUMNS[col]
        if not editable:
            return False
        entry = self._tools[row]
        old_value = getattr(entry, field_name)
        try:
            if field_name == "comment":
                new_value = str(value)
            elif field_name in ("pocket", "orientation"):
                new_value = int(value)
            else:
                new_value = float(value)
        except (ValueError, TypeError):
            return False
        if new_value == old_value:
            return False
        if self._command is not None:
            self._command.update_tool(entry.tool_id, **{field_name: new_value})
        self.dataChanged.emit(index, index, [role])
        return True


class ToolOffsetView(QtcncWidget, QWidget):
    """Tool offset editor backed by the daemon's tool database."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._model = ToolOffsetModel(self)
        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setStretchLastSection(True)

        self._add_btn = QPushButton("Add Tool", self)
        self._remove_btn = QPushButton("Remove Tool", self)
        self._refresh_btn = QPushButton("Refresh", self)
        self._add_btn.clicked.connect(self._on_add)
        self._remove_btn.clicked.connect(self._on_remove)
        self._refresh_btn.clicked.connect(self._refresh)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self._add_btn)
        toolbar.addWidget(self._remove_btn)
        toolbar.addWidget(self._refresh_btn)
        toolbar.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addLayout(toolbar)
        layout.addWidget(self._table, 1)

    def qtcnc_setup(self) -> None:
        cmd = self.window().qtcnc_command
        self._model.set_command(cmd)
        self.connect_status("tool_table_changed", self._on_tool_table_changed)
        self._refresh()

    def _refresh(self) -> None:
        cmd = getattr(self.window(), "qtcnc_command", None)
        if cmd is None:
            return
        try:
            result = cmd.get_tool_db()
        except Exception:
            return
        self._model.set_tools(result.tools)

    def _on_tool_table_changed(self, *args: Any) -> None:
        self._refresh()

    def _on_add(self) -> None:
        cmd = getattr(self.window(), "qtcnc_command", None)
        if cmd is None:
            return
        tool_id, ok = QInputDialog.getInt(
            self, "Add Tool", "Tool number:", 1, 1, 99999,
        )
        if not ok:
            return
        existing = {t.tool_id for t in self._model.tools}
        if tool_id in existing:
            QMessageBox.warning(
                self, "Duplicate", f"Tool T{tool_id} already exists.",
            )
            return
        cmd.add_tool(tool_id, tool_id)
        self._refresh()

    def _on_remove(self) -> None:
        cmd = getattr(self.window(), "qtcnc_command", None)
        if cmd is None:
            return
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return
        row = indexes[0].row()
        tools = self._model.tools
        if row < 0 or row >= len(tools):
            return
        entry = tools[row]
        cmd.remove_tool(entry.tool_id)
        self._refresh()

    @property
    def model(self) -> ToolOffsetModel:
        return self._model
