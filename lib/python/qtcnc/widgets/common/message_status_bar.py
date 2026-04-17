"""MessageStatusBar: single-line status display for INFO messages."""

from __future__ import annotations

from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QFrame, QHBoxLayout, QLabel

from qtcnc.core.types import Message, MessageSeverity
from qtcnc.widgets.base import QtcncWidget

_BORDER_COLORS = {
    MessageSeverity.DEBUG: "#888888",
    MessageSeverity.INFO: "#2196F3",
    MessageSeverity.WARNING: "#FF9800",
    MessageSeverity.ERROR: "#F44336",
}

_FADE_TIMEOUT_MS = 30_000


class MessageStatusBar(QtcncWidget, QFrame):
    """Displays the most recent INFO-level message with a severity border."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._label = QLabel(self)
        self._label.setWordWrap(False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.addWidget(self._label)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self._fade)
        self._set_border(MessageSeverity.INFO)

    def qtcnc_setup(self) -> None:
        bus = getattr(self.window(), "qtcnc_messages", None)
        if bus is not None:
            bus.bar_message.connect(self._on_bar_message)

    def _on_bar_message(self, msg: Message) -> None:
        self._label.setText(msg.text)
        self._set_border(msg.severity)
        self._label.setStyleSheet("")
        self._fade_timer.start(_FADE_TIMEOUT_MS)

    def _set_border(self, severity: MessageSeverity) -> None:
        color = _BORDER_COLORS.get(severity, "#2196F3")
        self.setStyleSheet(
            f"MessageStatusBar {{ border-left: 3px solid {color}; }}"
        )

    def _fade(self) -> None:
        self._label.setStyleSheet("color: #888888;")
