"""QtcncWidget: mixin for every qtcnc custom widget.

Widgets declare their HAL pin needs as a class attribute:

    class DroWidget(QtcncWidget, QLabel):
        HAL_PINS = [
            HalPinSpec(name="qtcnc.{name}.value-out", type=HalType.FLOAT, dir=HalDir.OUT),
        ]

`{name}` is substituted with the widget's `objectName()` at bootstrap
time. Bootstrap walks the widget tree, collects every pin spec, dedupes,
and sends a single DECLARE_PINS message to the daemon.

**QtcncWidget is a pure Python mixin, not a QWidget subclass.** This is
deliberate: PyQt/PySide do not support multiple inheritance across two
Qt-derived classes (that would yield two independent QObject bases at
the C++ level). Subclasses combine QtcncWidget with one Qt widget class:
`class Foo(QtcncWidget, QPushButton)`, `class Bar(QtcncWidget, QLabel)`,
etc. Python MRO routes `super().__init__()` into the Qt class.

Instance-side API:

* `self.pin(suffix)` — look up a previously-created HalPin proxy by the
  suffix that follows `qtcnc.<objectName>.`
* `self.connect_status(name, slot)` — connect a Status signal to a slot
  using `Qt.QueuedConnection`. Tracked so the widget can detach cleanly.
* `closeEvent` disconnects everything tracked.

Widget modules MUST NOT import from `qtcnc.transport.zmq_*` or from
`linuxcnc` / `hal`. Qt Designer loads plugin code in a minimal process
where those imports would explode.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from qtpy.QtCore import Qt

from qtcnc.core.hal_spec import HalPinSpec


class QtcncWidget:
    """Mixin. Combine with a QWidget subclass: `class Foo(QtcncWidget, QLabel)`."""

    HAL_PINS: ClassVar[list[HalPinSpec]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._status_connections: list[tuple[Any, Any]] = []
        self._hal_pin_connections: list[Any] = []
        self._qtcnc_setup_done: bool = False

    def qtcnc_setup(self) -> None:
        """Wire signals and HAL pins. Called by bootstrap after DECLARE_PINS ACKs.

        Override to do your widget-specific wiring: `self.connect_status(...)`,
        `self.pin(...).value_changed.connect(...)`, etc. This hook runs exactly
        once per widget; bootstrap skips widgets whose `_qtcnc_setup_done` is
        already True.
        """
        pass

    # ----- pin declaration -----

    @classmethod
    def declared_pins(cls, instance: "QtcncWidget") -> list[HalPinSpec]:
        """Return HAL_PINS with `{name}` substituted from the instance's objectName()."""
        name = instance.objectName() or ""
        if not name:
            raise ValueError(
                f"{cls.__name__} has no objectName(); "
                "set one in Designer or via setObjectName() before bootstrap"
            )
        out: list[HalPinSpec] = []
        for spec in cls.HAL_PINS:
            out.append(spec.substitute(name))
        return out

    # ----- instance helpers -----

    def pin(self, suffix: str) -> Any:
        """Return the HalPin for this widget's `qtcnc.<name>.<suffix>` pin.

        Raises KeyError if the pin hasn't been registered yet (bootstrap
        is expected to have populated the hub by the time widgets call
        `self.pin()`).
        """
        hub = self._hal_hub()
        full = f"qtcnc.{self.objectName()}.{suffix}"
        if full not in hub:
            raise KeyError(f"no HAL pin named {full!r} for this widget")
        return hub[full]

    def connect_status(self, signal_name: str, slot: Any) -> None:
        """Connect a Status signal to `slot` using Qt.QueuedConnection."""
        status = self._status()
        sig = getattr(status, signal_name, None)
        if sig is None:
            raise AttributeError(f"Status has no signal {signal_name!r}")
        conn = sig.connect(slot, Qt.QueuedConnection)
        self._status_connections.append((sig, conn))

    # ----- internal lookups -----

    def _status(self) -> Any:
        win = self.window()
        status = getattr(win, "qtcnc_status", None)
        if status is None:
            raise RuntimeError(
                "qtcnc_status not attached to window; bootstrap did not run"
            )
        return status

    def _hal_hub(self) -> Any:
        win = self.window()
        hub = getattr(win, "qtcnc_hal", None)
        if hub is None:
            raise RuntimeError(
                "qtcnc_hal not attached to window; bootstrap did not run"
            )
        return hub

    # ----- cleanup -----

    def closeEvent(self, event: Any) -> None:
        for sig, conn in self._status_connections:
            try:
                sig.disconnect(conn)
            except (TypeError, RuntimeError):
                pass
        self._status_connections.clear()
        super().closeEvent(event)


def collect_pin_declarations(
    widgets: list[QtcncWidget],
) -> list[HalPinSpec]:
    """Walk a list of widget instances and return merged HalPinSpec list.

    Raises `PinCollision` if two widgets ask for the same pin name.
    """
    out: list[HalPinSpec] = []
    seen: dict[str, str] = {}  # pin name -> offending widget objectName
    for w in widgets:
        for spec in type(w).declared_pins(w):
            if spec.name in seen:
                raise PinCollision(
                    f"HAL pin {spec.name!r} declared by both "
                    f"{seen[spec.name]!r} and {w.objectName()!r}"
                )
            seen[spec.name] = w.objectName()
            out.append(spec)
    return out


class PinCollision(ValueError):
    """Two widgets declared the same HAL pin name."""
