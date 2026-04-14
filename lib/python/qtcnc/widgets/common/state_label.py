"""StateLabel: shows the current TaskState as a text label."""

from __future__ import annotations

from typing import Any

from qtpy.QtWidgets import QLabel

from qtcnc.core.types import TaskState
from qtcnc.widgets.base import QtcncWidget


_TASK_STATE_TEXT: dict[TaskState, str] = {
    TaskState.ESTOP: "ESTOPPED",
    TaskState.ESTOP_RESET: "ESTOP RESET",
    TaskState.OFF: "OFF",
    TaskState.ON: "ON",
}


class StateLabel(QtcncWidget, QLabel):
    """Label bound to `status.task_state_changed`."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._state: TaskState = TaskState.ESTOP
        self._refresh()

    def qtcnc_setup(self) -> None:
        self.connect_status("task_state_changed", self._on_state_changed)
        self._on_state_changed(self.window().qtcnc_status.state.task_state)

    def _on_state_changed(self, state: TaskState) -> None:
        self._state = state
        self._refresh()

    def _refresh(self) -> None:
        self.setText(_TASK_STATE_TEXT.get(self._state, str(self._state)))
