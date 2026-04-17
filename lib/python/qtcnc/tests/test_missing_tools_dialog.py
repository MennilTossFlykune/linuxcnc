"""Tests for MissingToolsDialog."""

from __future__ import annotations

import sys

import pytest

from qtpy.QtWidgets import QApplication

from qtcnc.widgets.common.missing_tools_dialog import MissingToolsDialog


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class TestMissingToolsDialog:
    def test_row_count_matches_missing(self):
        dlg = MissingToolsDialog({5, 10, 15})
        assert len(dlg.rows) == 3

    def test_all_checked_by_default(self):
        dlg = MissingToolsDialog({1, 2, 3})
        for cb, tid, spin in dlg.rows:
            assert cb.isChecked()

    def test_selected_tools_returns_checked(self):
        dlg = MissingToolsDialog({5, 10})
        result = dlg.selected_tools()
        tool_ids = sorted(tid for tid, pocket in result)
        assert tool_ids == [5, 10]

    def test_default_pocket_equals_tool_id(self):
        dlg = MissingToolsDialog({7, 12})
        for cb, tid, spin in dlg.rows:
            assert spin.value() == tid

    def test_uncheck_excludes_from_selection(self):
        dlg = MissingToolsDialog({1, 2, 3})
        for cb, tid, spin in dlg.rows:
            if tid == 2:
                cb.setChecked(False)
        result = dlg.selected_tools()
        assert sorted(tid for tid, _ in result) == [1, 3]

    def test_pocket_override(self):
        dlg = MissingToolsDialog({5})
        cb, tid, spin = dlg.rows[0]
        spin.setValue(99)
        result = dlg.selected_tools()
        assert result == [(5, 99)]

    def test_empty_set_produces_no_rows(self):
        dlg = MissingToolsDialog(set())
        assert len(dlg.rows) == 0
        assert dlg.selected_tools() == []

    def test_rows_sorted_by_tool_id(self):
        dlg = MissingToolsDialog({30, 10, 20})
        tids = [tid for _, tid, _ in dlg.rows]
        assert tids == [10, 20, 30]

    def test_frozenset_accepted(self):
        dlg = MissingToolsDialog(frozenset({1, 2}))
        assert len(dlg.rows) == 2
