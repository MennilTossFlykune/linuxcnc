"""HalPin: Qt-facing proxy for a single HAL pin.

A `HalPin` is cheap: one QObject with a single `value_changed` signal
and cached current value. Widgets get pin proxies via the
`HalPinHub.get_or_create(name)` method; the hub is a single callback
sink for the Transport's `on_hal_pin_update` stream and fans out to
individual pins by name.

Writes go through `HalPin.set(value)` which calls `transport.write_pin`.
Reads happen via the cached `value` property or via `value_changed`
signal subscription.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtCore import QObject, Signal

from qtcnc.transport.base import Transport


class HalPin(QObject):
    """Client-side proxy for one HAL pin."""

    value_changed = Signal(object)

    def __init__(self, name: str, hub: "HalPinHub", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._hub = hub
        self._value: Any = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def value(self) -> Any:
        return self._value

    def set(self, value: Any) -> None:
        """Write a new value to the pin via the Transport."""
        self._hub.write(self._name, value)

    def _update(self, value: Any) -> None:
        """Accept a value pushed from the Transport and emit the Qt signal."""
        self._value = value
        self.value_changed.emit(value)


class HalPinHub(QObject):
    """Single on_hal_pin_update callback sink; fans out to HalPin proxies."""

    def __init__(self, transport: Transport, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._transport = transport
        self._pins: dict[str, HalPin] = {}
        self._transport.set_on_hal_pin_update(self._on_update)

    def get_or_create(self, name: str) -> HalPin:
        pin = self._pins.get(name)
        if pin is None:
            pin = HalPin(name, self, parent=self)
            self._pins[name] = pin
        return pin

    def __contains__(self, name: str) -> bool:
        return name in self._pins

    def __getitem__(self, name: str) -> HalPin:
        return self._pins[name]

    def write(self, name: str, value: Any) -> None:
        self._transport.write_pin(name, value)

    def _on_update(self, name: str, value: Any) -> None:
        pin = self._pins.get(name)
        if pin is not None:
            pin._update(value)
