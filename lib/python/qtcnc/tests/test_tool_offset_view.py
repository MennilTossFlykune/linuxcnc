"""Tests for ToolOffsetView and ToolOffsetModel."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from qtpy.QtCore import QModelIndex, Qt
from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.core.command import Command
from qtcnc.core.status import Status
from qtcnc.core.types import ToolDbEntry
from qtcnc.signals import CommandVerb
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.base import QtcncWidget, collect_pin_declarations
from qtcnc.widgets.common.tool_offset_view import ToolOffsetModel, ToolOffsetView
from qtcnc.widgets.hal import HalPinHub


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class _RecordingMock(MockTransport):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[CommandVerb, dict[str, Any]]] = []

    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        self.commands.append((verb, kwargs))
        super().exec_command(verb, **kwargs)


def _make_world():
    t = _RecordingMock()
    window = QMainWindow()
    widget = ToolOffsetView(window)
    widget.setObjectName("tool_table")
    status = Status(t)
    hub = HalPinHub(t)
    cmd = Command(t)
    window.qtcnc_status = status
    window.qtcnc_command = cmd
    window.qtcnc_hal = hub
    t.hello()
    status.bootstrap()
    widget.qtcnc_setup()
    return t, status, cmd, window, widget


class TestToolOffsetModel:
    def test_empty_model_has_zero_rows(self):
        model = ToolOffsetModel()
        assert model.rowCount() == 0

    def test_set_tools_populates_rows(self):
        model = ToolOffsetModel()
        tools = [
            ToolDbEntry(tool_id=1, pocket=1, z_offset=-25.4, diameter=6.0),
            ToolDbEntry(tool_id=2, pocket=2),
        ]
        model.set_tools(tools)
        assert model.rowCount() == 2

    def test_column_count_matches_schema(self):
        model = ToolOffsetModel()
        assert model.columnCount() == 16

    def test_data_returns_tool_id(self):
        model = ToolOffsetModel()
        model.set_tools([ToolDbEntry(tool_id=5, pocket=5)])
        idx = model.index(0, 0)
        assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "5"

    def test_data_returns_formatted_float(self):
        model = ToolOffsetModel()
        model.set_tools([ToolDbEntry(tool_id=1, pocket=1, z_offset=-25.4)])
        idx = model.index(0, 4)  # z_offset column
        assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "-25.4000"

    def test_tool_id_not_editable(self):
        model = ToolOffsetModel()
        model.set_tools([ToolDbEntry(tool_id=1, pocket=1)])
        idx = model.index(0, 0)
        flags = model.flags(idx)
        assert not (flags & Qt.ItemFlag.ItemIsEditable)

    def test_pocket_is_editable(self):
        model = ToolOffsetModel()
        model.set_tools([ToolDbEntry(tool_id=1, pocket=1)])
        idx = model.index(0, 1)
        flags = model.flags(idx)
        assert flags & Qt.ItemFlag.ItemIsEditable

    def test_set_data_dispatches_update(self):
        t = _RecordingMock()
        t.hello()
        t.add_tool(3, 3, z_offset=0.0)
        cmd = Command(t)
        model = ToolOffsetModel()
        model.set_command(cmd)
        model.set_tools([ToolDbEntry(tool_id=3, pocket=3, z_offset=0.0)])
        idx = model.index(0, 4)  # z_offset
        result = model.setData(idx, -10.5, Qt.ItemDataRole.EditRole)
        assert result is True
        db = cmd.get_tool_db()
        t3 = [e for e in db.tools if e.tool_id == 3]
        assert len(t3) == 1
        assert t3[0].z_offset == -10.5

    def test_set_data_same_value_returns_false(self):
        model = ToolOffsetModel()
        model.set_tools([ToolDbEntry(tool_id=1, pocket=1, z_offset=-5.0)])
        idx = model.index(0, 4)
        result = model.setData(idx, -5.0, Qt.ItemDataRole.EditRole)
        assert result is False

    def test_set_data_invalid_value_returns_false(self):
        model = ToolOffsetModel()
        model.set_tools([ToolDbEntry(tool_id=1, pocket=1)])
        idx = model.index(0, 4)  # z_offset
        result = model.setData(idx, "not_a_number", Qt.ItemDataRole.EditRole)
        assert result is False

    def test_header_data(self):
        model = ToolOffsetModel()
        assert model.headerData(0, Qt.Orientation.Horizontal) == "Tool #"
        assert model.headerData(4, Qt.Orientation.Horizontal) == "Z"
        assert model.headerData(15, Qt.Orientation.Horizontal) == "Comment"

    def test_comment_edit(self):
        t = _RecordingMock()
        t.hello()
        cmd = Command(t)
        model = ToolOffsetModel()
        model.set_command(cmd)
        model.set_tools([ToolDbEntry(tool_id=1, pocket=1, comment="old")])
        idx = model.index(0, 15)
        result = model.setData(idx, "new comment", Qt.ItemDataRole.EditRole)
        assert result is True
        db = cmd.get_tool_db()
        t1 = [e for e in db.tools if e.tool_id == 1]
        assert len(t1) == 1
        assert t1[0].comment == "new comment"


class TestToolOffsetView:
    def test_widget_populates_on_setup(self):
        t, status, cmd, window, widget = _make_world()
        assert widget.model.rowCount() >= 2

    def test_refresh_updates_model(self):
        t, status, cmd, window, widget = _make_world()
        cmd.add_tool(99, 99, z_offset=-1.0)
        widget._refresh()
        ids = [tool.tool_id for tool in widget.model.tools]
        assert 99 in ids

    def test_remove_dispatches_to_transport(self):
        t, status, cmd, window, widget = _make_world()
        initial = widget.model.tools
        assert len(initial) > 0
        first_id = initial[0].tool_id
        cmd.remove_tool(first_id)
        widget._refresh()
        ids = [tool.tool_id for tool in widget.model.tools]
        assert first_id not in ids

    def test_tool_table_changed_signal_triggers_refresh(self):
        t, status, cmd, window, widget = _make_world()
        cmd.add_tool(77, 77, z_offset=-3.0)
        ids_before = {tool.tool_id for tool in widget.model.tools}
        assert 77 not in ids_before
        status.tool_table_changed.emit(())
        QApplication.processEvents()
        ids_after = {tool.tool_id for tool in widget.model.tools}
        assert 77 in ids_after
