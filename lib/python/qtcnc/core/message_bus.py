"""MessageBus: routes Messages to display surfaces by severity.

Toast gets ERROR and WARNING. Status bar gets INFO. Log gets everything.
No message appears on more than one display surface (except the log,
which always records).
"""

from __future__ import annotations

import time

from qtpy.QtCore import QObject, Signal

from qtcnc.core.types import Message, MessageSeverity, MessageSource


class MessageBus(QObject):
    """Central routing hub for all operator-facing messages."""

    toast_message = Signal(object)   # Message — ERROR, WARNING
    bar_message = Signal(object)     # Message — INFO
    log_message = Signal(object)     # Message — all severities

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def connect_status(self, status: QObject) -> None:
        """Wire the Status.message signal into the bus."""
        status.message.connect(self._route)

    def post(
        self,
        severity: MessageSeverity,
        source: MessageSource,
        text: str,
    ) -> None:
        msg = Message(
            severity=severity,
            source=source,
            text=text,
            timestamp=time.time(),
        )
        self._route(msg)

    def _route(self, msg: Message) -> None:
        self.log_message.emit(msg)
        if msg.severity >= MessageSeverity.WARNING:
            self.toast_message.emit(msg)
        elif msg.severity == MessageSeverity.INFO:
            self.bar_message.emit(msg)
