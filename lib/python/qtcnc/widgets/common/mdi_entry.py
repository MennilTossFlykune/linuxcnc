"""MdiEntry: QLineEdit bound to Command.mdi().

Drop one into a screen, set ``submit_on_enter`` (default True) and the
widget will fire ``cmd.mdi(<text>)`` whenever the operator presses Return
inside the line edit. The entry auto-disables whenever ``state.can_mdi``
is False — estopped, not in MDI mode, or the interpreter is still busy
executing a previous MDI line — so the same ``can_mdi`` predicate that
gates ActionButton gates the entry.

Widgets pair naturally with a dedicated run button: an ``ActionButton``
with ``action="mdi_entry"`` and ``target`` set to the entry's
objectName forwards a click into ``MdiEntry.execute()``. Both share the
shared ``can_mdi`` gate so they enable and disable together.

``execute()`` is the single submission path — ``returnPressed`` routes
through it when ``submit_on_enter`` is on, and the companion button
dispatches to it on click. An empty entry or a non-``can_mdi`` state is
a silent no-op so accidental Return presses don't stall anything.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtCore import Property
from qtpy.QtWidgets import QLineEdit

from qtcnc.widgets.base import QtcncWidget


class MdiEntry(QtcncWidget, QLineEdit):
    """Line-edit that submits its text as an MDI command."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._submit_on_enter: bool = True
        self._clear_on_submit: bool = True
        self.setPlaceholderText("MDI")
        self.returnPressed.connect(self._on_return_pressed)

    # ----- Qt properties -----

    def _get_submit_on_enter(self) -> bool:
        return self._submit_on_enter

    def _set_submit_on_enter(self, value: bool) -> None:
        self._submit_on_enter = bool(value)

    submit_on_enter = Property(bool, _get_submit_on_enter, _set_submit_on_enter)

    def _get_clear_on_submit(self) -> bool:
        return self._clear_on_submit

    def _set_clear_on_submit(self, value: bool) -> None:
        self._clear_on_submit = bool(value)

    clear_on_submit = Property(bool, _get_clear_on_submit, _set_clear_on_submit)

    # ----- qtcnc lifecycle -----

    def qtcnc_setup(self) -> None:
        self.connect_status("machine_state_changed", self._on_state_change)
        self._refresh()

    def _on_state_change(self, *args: Any) -> None:
        self._refresh()

    def _refresh(self) -> None:
        state = self.window().qtcnc_status.state
        self.setEnabled(state.can_mdi)

    # ----- submission -----

    def execute(self) -> None:
        """Fire the current text as an MDI command.

        No-op on empty text or when the shared `can_mdi` predicate
        refuses. Clears the entry afterwards if `clear_on_submit` is on.
        """
        text = self.text().strip()
        if not text:
            return
        win = self.window()
        state = win.qtcnc_status.state
        if not state.can_mdi:
            return
        win.qtcnc_command.mdi(text)
        if self._clear_on_submit:
            self.clear()

    def _on_return_pressed(self) -> None:
        if self._submit_on_enter:
            self.execute()
