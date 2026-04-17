"""MessageLog: scrollable, filterable table of all messages."""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime

from qtpy.QtCore import QAbstractTableModel, QModelIndex, Qt
from qtpy.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from qtcnc.core.types import Message, MessageSeverity, MessageSource
from qtcnc.widgets.base import QtcncWidget

_MAX_ROWS = 1000

_SEVERITY_NAMES = {
    MessageSeverity.DEBUG: "DEBUG",
    MessageSeverity.INFO: "INFO",
    MessageSeverity.WARNING: "WARNING",
    MessageSeverity.ERROR: "ERROR",
}

_SOURCE_NAMES = {
    MessageSource.LINUXCNC: "LinuxCNC",
    MessageSource.FRAMEWORK: "Framework",
    MessageSource.USER: "User",
}

_COLUMNS = ("Time", "Source", "Level", "Message")


class _MessageModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: deque[Message] = deque(maxlen=_MAX_ROWS)
        self._filtered: list[Message] = []
        self._source_filter: MessageSource | None = None
        self._severity_filter: MessageSeverity | None = None

    def set_source_filter(self, source: MessageSource | None) -> None:
        self._source_filter = source
        self._rebuild_filtered()

    def set_severity_filter(self, severity: MessageSeverity | None) -> None:
        self._severity_filter = severity
        self._rebuild_filtered()

    def append(self, msg: Message) -> None:
        was_full = len(self._items) == self._items.maxlen
        self._items.append(msg)
        if was_full:
            self._rebuild_filtered()
        elif self._passes(msg):
            row = len(self._filtered)
            self.beginInsertRows(QModelIndex(), row, row)
            self._filtered.append(msg)
            self.endInsertRows()

    def _passes(self, msg: Message) -> bool:
        if self._source_filter is not None and msg.source != self._source_filter:
            return False
        if self._severity_filter is not None and msg.severity < self._severity_filter:
            return False
        return True

    def _rebuild_filtered(self) -> None:
        self.beginResetModel()
        self._filtered = [m for m in self._items if self._passes(m)]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        msg = self._filtered[index.row()]
        col = index.column()
        if col == 0:
            return datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
        if col == 1:
            return _SOURCE_NAMES.get(msg.source, str(msg.source))
        if col == 2:
            return _SEVERITY_NAMES.get(msg.severity, str(msg.severity))
        if col == 3:
            return msg.text
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _COLUMNS[section]
        return None


class MessageLog(QtcncWidget, QWidget):
    """Scrollable message history with source and severity filters."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self._model = _MessageModel(self)
        self._auto_scroll = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        filter_row = QHBoxLayout()
        self._source_combo = QComboBox(self)
        self._source_combo.addItem("All Sources", None)
        for src in MessageSource:
            self._source_combo.addItem(_SOURCE_NAMES[src], src)
        self._source_combo.currentIndexChanged.connect(self._on_source_filter)
        filter_row.addWidget(self._source_combo)

        self._severity_combo = QComboBox(self)
        self._severity_combo.addItem("All Levels", None)
        for sev in MessageSeverity:
            self._severity_combo.addItem(_SEVERITY_NAMES[sev], sev)
        self._severity_combo.currentIndexChanged.connect(self._on_severity_filter)
        filter_row.addWidget(self._severity_combo)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table)

        vbar = self._table.verticalScrollBar()
        vbar.valueChanged.connect(self._on_scroll)

    def qtcnc_setup(self) -> None:
        bus = getattr(self.window(), "qtcnc_messages", None)
        if bus is not None:
            bus.log_message.connect(self._on_log_message)

    def _on_log_message(self, msg: Message) -> None:
        self._model.append(msg)
        if self._auto_scroll:
            self._table.scrollToBottom()

    def _on_source_filter(self, index: int) -> None:
        data = self._source_combo.itemData(index)
        self._model.set_source_filter(data)

    def _on_severity_filter(self, index: int) -> None:
        data = self._severity_combo.itemData(index)
        self._model.set_severity_filter(data)

    def _on_scroll(self, value: int) -> None:
        vbar = self._table.verticalScrollBar()
        self._auto_scroll = (value >= vbar.maximum() - 1)
