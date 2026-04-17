"""ToastOverlay: floating notification stack for ERROR and WARNING messages.

Toasts are parented directly to the main window and positioned
absolutely in the top-right corner so they float above all other
widgets without blocking input to anything underneath.
"""

from __future__ import annotations

from qtpy.QtCore import QEvent, QObject, Qt, QTimer
from qtpy.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from qtcnc.core.types import Message, MessageSeverity

_MAX_VISIBLE = 3
_WARNING_TIMEOUT_MS = 5_000
_MARGIN = 12

_BORDER_COLORS = {
    MessageSeverity.WARNING: "#FF9800",
    MessageSeverity.ERROR: "#F44336",
}


class _Toast(QFrame):
    """One toast notification, parented to the main window."""

    def __init__(self, msg: Message, manager: "ToastOverlay", parent: QWidget) -> None:
        super().__init__(parent)
        self.msg = msg
        self._manager = manager
        color = _BORDER_COLORS.get(msg.severity, "#F44336")
        self.setStyleSheet(
            f"_Toast {{ background: #2B2B2B; border-left: 4px solid {color};"
            f" border-radius: 4px; }}"
        )
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 4, 6)

        label = QLabel(msg.text, self)
        label.setWordWrap(True)
        label.setStyleSheet("color: #EEEEEE; background: transparent;")
        label.setMaximumWidth(350)
        layout.addWidget(label, stretch=1)

        close_btn = QPushButton("\u2715", self)
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet(
            "QPushButton { color: #AAAAAA; background: transparent;"
            " border: none; font-size: 14px; }"
            "QPushButton:hover { color: #FFFFFF; }"
        )
        close_btn.clicked.connect(self._dismiss)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignTop)

        self.adjustSize()

    def _dismiss(self) -> None:
        self._manager._remove_toast(self)


class ToastOverlay(QObject):
    """Non-visual manager that creates and positions toast widgets.

    Toasts are parented directly to the target window so they float
    above everything without blocking mouse events to widgets below.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._window = parent
        self._toasts: list[_Toast] = []
        parent.installEventFilter(self)

    def connect_bus(self, bus) -> None:
        bus.toast_message.connect(self._on_toast_message)

    def _on_toast_message(self, msg: Message) -> None:
        while len(self._toasts) >= _MAX_VISIBLE:
            oldest = self._toasts[0]
            self._remove_toast(oldest)

        toast = _Toast(msg, self, self._window)
        toast.raise_()
        toast.show()
        self._toasts.append(toast)
        self._relayout()

        if msg.severity == MessageSeverity.WARNING:
            QTimer.singleShot(_WARNING_TIMEOUT_MS, lambda t=toast: self._auto_dismiss(t))

    def _auto_dismiss(self, toast: _Toast) -> None:
        try:
            if toast in self._toasts:
                self._remove_toast(toast)
        except RuntimeError:
            pass

    def _remove_toast(self, toast: _Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        try:
            toast.hide()
            toast.deleteLater()
        except RuntimeError:
            pass
        self._relayout()

    def _relayout(self) -> None:
        w = self._window.width()
        y = _MARGIN
        for toast in self._toasts:
            toast.adjustSize()
            x = w - toast.width() - _MARGIN
            toast.move(max(0, x), y)
            toast.raise_()
            y += toast.height() + 6

    def eventFilter(self, obj, event) -> bool:
        if obj is getattr(self, "_window", None) and event.type() == QEvent.Type.Resize:
            self._relayout()
        return False
