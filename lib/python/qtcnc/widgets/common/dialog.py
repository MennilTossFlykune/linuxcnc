"""QtcncDialog: base dialog for all qtcnc popup interactions.

Provides a consistent layout (message label, content area, button box)
and behaviour properties that subclasses configure:

- ``severity``  — drives border/header colour (INFO, WARNING, ERROR)
- ``blocking``  — modal (blocks parent) vs modeless
- ``frameless`` — no window decorations, for kiosk/touchscreen setups
- ``mandatory`` — disables close button and Escape key; the operator
  must use one of the explicit buttons to dismiss
- ``timeout``   — auto-dismiss after N milliseconds (0 = never)

HAL pin support
---------------

Dialogs that interact with the HAL world (e.g. manual tool change)
declare pins via a class attribute, the same pattern as QtcncWidget::

    class ManualToolChange(QtcncDialog):
        HAL_PINS = [
            HalPinSpec(name="qtcnc.{name}.change", type=HalType.BIT, dir=HalDir.IN),
            HalPinSpec(name="qtcnc.{name}.changed", type=HalType.BIT, dir=HalDir.OUT),
            HalPinSpec(name="qtcnc.{name}.number", type=HalType.S32, dir=HalDir.IN),
        ]
        TRIGGER_PIN = "change"
        RESPONSE_PIN = "changed"

``TRIGGER_PIN`` names the suffix of an IN pin whose rising edge shows
the dialog. ``RESPONSE_PIN`` names the suffix of an OUT pin that is
set True when the dialog is accepted and False when rejected.

Because dialogs are not embedded in the .ui widget tree, bootstrap
cannot discover them automatically. The handler is responsible for:

1. Including the dialog's pin specs in ``declare_pins()``::

       def declare_pins(self) -> list[HalPinSpec]:
           return ManualToolChange.declared_pins("tool_change")

2. Calling ``dialog.hal_setup(hub)`` after construction so the
   trigger/response wiring activates.
"""

from __future__ import annotations

from dataclasses import replace
from enum import IntEnum
from typing import Any, ClassVar

from qtpy.QtCore import Qt, QTimer
from qtpy.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from qtcnc.core.hal_spec import HalPinSpec


class Severity(IntEnum):
    INFO = 0
    WARNING = 1
    ERROR = 2


_BORDER_COLORS: dict[Severity, str] = {
    Severity.INFO: "#3daee9",
    Severity.WARNING: "#f67400",
    Severity.ERROR: "#da4453",
}

_HEADER_COLORS: dict[Severity, str] = {
    Severity.INFO: "#2980b9",
    Severity.WARNING: "#c9700a",
    Severity.ERROR: "#c0392b",
}


class QtcncDialog(QDialog):
    """Base dialog for qtcnc operator interactions.

    Subclasses override ``build_content(layout)`` to insert widgets
    between the message label and the button box.  The base class
    handles severity styling, modality, timeout, the mandatory /
    frameless flags, and optional HAL pin wiring.
    """

    HAL_PINS: ClassVar[list[HalPinSpec]] = []
    TRIGGER_PIN: ClassVar[str | None] = None
    RESPONSE_PIN: ClassVar[str | None] = None

    def __init__(
        self,
        parent: Any = None,
        *,
        title: str = "",
        message: str = "",
        severity: Severity = Severity.INFO,
        blocking: bool = True,
        frameless: bool = False,
        mandatory: bool = False,
        timeout: int = 0,
        buttons: QDialogButtonBox.StandardButton = (
            QDialogButtonBox.StandardButton.Ok
        ),
    ) -> None:
        flags = Qt.WindowType.Dialog
        if frameless:
            flags |= Qt.WindowType.FramelessWindowHint
        super().__init__(parent, flags)

        self._severity = severity
        self._mandatory = mandatory
        self._timeout = timeout
        self._timer: QTimer | None = None
        self._hal_name: str | None = None
        self._hal_hub: Any = None
        self._trigger_dismissed = False
        self._trigger_high = False

        if title:
            self.setWindowTitle(title)

        if mandatory:
            self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

        if blocking:
            self.setWindowModality(Qt.WindowModality.ApplicationModal)
        else:
            self.setWindowModality(Qt.WindowModality.NonModal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QLabel(self)
        self._header.setWordWrap(True)
        self._header.setContentsMargins(12, 10, 12, 10)
        layout.addWidget(self._header)

        self._content_layout = QVBoxLayout()
        self._content_layout.setContentsMargins(12, 8, 12, 8)
        layout.addLayout(self._content_layout, 1)

        if message:
            self._header.setText(message)
        else:
            self._header.hide()

        self.build_content(self._content_layout)

        self._button_box = QDialogButtonBox(buttons, self)
        self._button_box.setContentsMargins(12, 4, 12, 10)
        self._button_box.accepted.connect(self.accept)
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)

        self._apply_severity_style()

    # ----- subclass hooks -----

    def build_content(self, layout: QVBoxLayout) -> None:
        """Override to populate the content area between message and buttons."""

    def on_trigger(self) -> None:
        """Called when the trigger pin goes True.

        Default implementation shows the dialog. Subclasses can override
        to update content before showing (e.g. read a tool-number pin).
        """
        self.show()

    # ----- HAL pin support -----

    @classmethod
    def declared_pins(cls, hal_name: str) -> list[HalPinSpec]:
        """Return HAL_PINS with ``{name}`` substituted.

        Unlike QtcncWidget (which reads objectName at bootstrap time),
        dialogs are not in the widget tree. The caller provides the
        ``hal_name`` — the identity string that expands ``{name}`` in
        pin templates.
        """
        out: list[HalPinSpec] = []
        for spec in cls.HAL_PINS:
            out.append(spec.substitute(hal_name))
        return out

    def hal_setup(self, hub: Any, hal_name: str) -> None:
        """Wire HAL trigger/response pins after construction.

        ``hub`` is the HalPinHub (``window.qtcnc_hal``).
        ``hal_name`` is the identity string used in ``declared_pins()``.
        """
        self._hal_hub = hub
        self._hal_name = hal_name
        if self.TRIGGER_PIN is not None:
            trigger = self.pin(self.TRIGGER_PIN)
            trigger.value_changed.connect(self._on_trigger_changed)
        if self.RESPONSE_PIN is not None:
            self.accepted.connect(self._on_accepted_hal)
            self.rejected.connect(self._on_rejected_hal)

    def pin(self, suffix: str) -> Any:
        """Look up a HAL pin by suffix: ``qtcnc.<hal_name>.<suffix>``."""
        if self._hal_hub is None or self._hal_name is None:
            raise RuntimeError(
                "hal_setup() has not been called on this dialog"
            )
        full = f"qtcnc.{self._hal_name}.{suffix}"
        return self._hal_hub[full]

    def _on_trigger_changed(self, value: Any) -> None:
        self._trigger_high = bool(value)
        if value:
            self._trigger_dismissed = False
            self.on_trigger()
        elif self.isVisible():
            self._trigger_dismissed = True
            self.reject()
        elif self.RESPONSE_PIN is not None:
            # Handshake completion: iocontrol drops `change` after seeing
            # `changed`; we must drop `changed` so the next cycle isn't
            # short-circuited by a stuck response pin.
            self.pin(self.RESPONSE_PIN).set(False)

    @property
    def trigger_high(self) -> bool:
        """True while the HAL trigger pin is asserted.

        Subclasses that defer ``show()`` (e.g. via ``QTimer.singleShot``)
        should check this before actually showing, so a trigger that
        dropped during the deferral doesn't leave an orphan dialog up.
        """
        return self._trigger_high

    def _on_accepted_hal(self) -> None:
        if self.RESPONSE_PIN is not None:
            self.pin(self.RESPONSE_PIN).set(True)

    def _on_rejected_hal(self) -> None:
        if self._trigger_dismissed:
            self._trigger_dismissed = False
            return
        if self.RESPONSE_PIN is not None:
            self.pin(self.RESPONSE_PIN).set(False)

    # ----- public API -----

    @property
    def severity(self) -> Severity:
        return self._severity

    @severity.setter
    def severity(self, value: Severity) -> None:
        self._severity = value
        self._apply_severity_style()

    @property
    def header(self) -> QLabel:
        return self._header

    @property
    def button_box(self) -> QDialogButtonBox:
        return self._button_box

    @property
    def content_layout(self) -> QVBoxLayout:
        return self._content_layout

    def button(self, std: QDialogButtonBox.StandardButton) -> Any:
        return self._button_box.button(std)

    # ----- overrides -----

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if self._timeout > 0 and self._timer is None:
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self.reject)
            self._timer.start(self._timeout)

    def keyPressEvent(self, event: Any) -> None:
        if self._mandatory and event.key() == Qt.Key.Key_Escape:
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: Any) -> None:
        if self._mandatory:
            event.ignore()
            return
        super().closeEvent(event)

    # ----- internals -----

    def _apply_severity_style(self) -> None:
        border = _BORDER_COLORS[self._severity]
        header_bg = _HEADER_COLORS[self._severity]
        self.setStyleSheet(
            f"QtcncDialog {{ border: 2px solid {border}; }}"
        )
        self._header.setStyleSheet(
            f"background: {header_bg}; color: white;"
            f" font-weight: bold;"
        )
