"""QtcncHandler: screen-level Python code with auto-wired signal hooks.

A handler file lives next to every screen's `.ui` and subclasses
`QtcncHandler`. The handler gets a `HandlerContext` at construction
time containing the window, Status, Command, WidgetTree, Config, and
HalPinHub. Override any of the `on_*` hooks you care about; bootstrap
auto-connects them to the matching Status signal.

`WidgetTree` wraps `window.findChild()` with `__getattr__` so `self.w.dro_x`
resolves the widget named `dro_x` in the loaded `.ui`. Typos produce a
clear `AttributeError` instead of a silent `None`.

Lifecycle hooks (called directly by bootstrap):

* `declare_pins()` — extra HalPinSpec list for the handler itself.
* `on_startup()` — after widgets load, before the window is shown.
* `on_ready()` — after the daemon ACKs DECLARE_PINS and the window is up.
* `on_shutdown()` — on window close.

Signal-routed hooks (auto-connected iff overridden):

* `on_estop(asserted: bool)` ← `status.estop_changed`
* `on_power(on: bool)` ← `status.power_changed`
* `on_mode_changed(mode: TaskMode)` ← `status.task_mode_changed`
* `on_error(severity, text)` ← `status.error`
* `on_file_loaded(path, program)` ← `status.program_loaded`
* `on_file_load_failed(path, reason)` ← `status.program_load_failed`
* `on_file_closed()` ← `status.program_closed`
* `on_file_missing(path)` ← `status.program_missing`
* `on_disconnected(reason)` ← `status.disconnected`
* `on_reconnected()` ← `status.connected` (fired every time, not just after drop)
* `on_device_added(device)` ← `devices.device_added` (v2)
* `on_device_removed(device)` ← `devices.device_removed` (v2)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QMainWindow, QWidget

from qtcnc.core.command import Command
from qtcnc.core.hal_spec import HalPinSpec
from qtcnc.core.status import Status
from qtcnc.core.types import ErrorSeverity, ProgramState, TaskMode
from qtcnc.widgets.hal import HalPinHub


class WidgetTree:
    """Attribute access over `window.findChild(QWidget, name)`, with caching.

    `self.w.dro_x` → the widget with `objectName() == "dro_x"`.
    `self.w.typo` → `AttributeError("no widget named 'typo'")`.
    """

    def __init__(self, root: QWidget) -> None:
        # Avoid triggering __setattr__ / __getattr__ recursion on _root.
        object.__setattr__(self, "_root", root)
        object.__setattr__(self, "_cache", {})

    def __getattr__(self, name: str) -> QWidget:
        if name.startswith("_"):
            raise AttributeError(name)
        cache: dict[str, QWidget] = object.__getattribute__(self, "_cache")
        cached = cache.get(name)
        if cached is not None:
            return cached
        root: QWidget = object.__getattribute__(self, "_root")
        found = root.findChild(QWidget, name)
        if found is None:
            raise AttributeError(f"no widget named {name!r}")
        cache[name] = found
        return found

    def __contains__(self, name: str) -> bool:
        try:
            self.__getattr__(name)
        except AttributeError:
            return False
        return True


@dataclass
class HandlerContext:
    """Everything a QtcncHandler needs from the bootstrap environment."""

    window: QMainWindow
    status: Status
    command: Command
    widgets: WidgetTree
    hal: HalPinHub
    config: Any = None  # QtcncConfig once that module exists
    devices: Any = None  # qtcnc.core.devices.Devices; None in tests that don't need it


class QtcncHandler:
    """Base handler. Subclass and override the hooks you care about."""

    def __init__(self, ctx: HandlerContext) -> None:
        self.ctx = ctx
        self.w = ctx.widgets
        self.window = ctx.window
        self.status = ctx.status
        self.cmd = ctx.command
        self.hal = ctx.hal
        self.config = ctx.config
        self.devices = ctx.devices

    # ----- Lifecycle (bootstrap calls these directly) -----

    def declare_pins(self) -> list[HalPinSpec]:
        return []

    def on_startup(self) -> None: ...
    def on_ready(self) -> None: ...
    def on_shutdown(self) -> None: ...

    # ----- Signal-driven hooks (auto-connected iff overridden) -----

    def on_estop(self, asserted: bool) -> None: ...
    def on_power(self, on: bool) -> None: ...
    def on_mode_changed(self, mode: TaskMode) -> None: ...
    def on_error(self, severity: ErrorSeverity, text: str) -> None: ...

    def on_file_loaded(self, path: str, program: ProgramState) -> None: ...
    def on_file_load_failed(self, path: str, reason: str) -> None: ...
    def on_file_closed(self) -> None: ...
    def on_file_missing(self, path: str) -> None: ...

    def on_device_added(self, device: Any) -> None: ...
    def on_device_removed(self, device: Any) -> None: ...
    def on_mount_added(self, mount: Any) -> None: ...
    def on_mount_removed(self, mount: Any) -> None: ...

    def on_disconnected(self, reason: str) -> None: ...
    def on_reconnected(self) -> None: ...


# ---------------------------------------------------------------------------
# Auto-connect
# ---------------------------------------------------------------------------


# Map of handler method name -> Status signal name.
# Only methods listed here are auto-connected. Lifecycle methods
# (on_startup, on_ready, on_shutdown, declare_pins) are called directly
# by bootstrap.
_HANDLER_SIGNALS: dict[str, str] = {
    "on_estop": "estop_changed",
    "on_power": "power_changed",
    "on_mode_changed": "task_mode_changed",
    "on_error": "error",
    "on_file_loaded": "program_loaded",
    "on_file_load_failed": "program_load_failed",
    "on_file_closed": "program_closed",
    "on_file_missing": "program_missing",
    "on_disconnected": "disconnected",
    "on_reconnected": "connected",
}

# Hooks routed through `ctx.devices` instead of `ctx.status`. Same
# override semantics — if the handler doesn't override, nothing is
# connected and `Devices` stays silent toward the handler.
_DEVICE_SIGNALS: dict[str, str] = {
    "on_device_added": "device_added",
    "on_device_removed": "device_removed",
    "on_mount_added": "mount_added",
    "on_mount_removed": "mount_removed",
}


def _is_overridden(handler: QtcncHandler, method_name: str) -> bool:
    cls_method = getattr(type(handler), method_name, None)
    base_method = getattr(QtcncHandler, method_name, None)
    return cls_method is not None and cls_method is not base_method


def auto_connect_handler(handler: QtcncHandler) -> list[Any]:
    """Connect every overridden on_* method to its Status signal.

    Returns a list of (signal, connection) tuples that the caller can
    disconnect later (on shutdown).
    """
    connections: list[Any] = []
    status = handler.status
    for method_name, signal_name in _HANDLER_SIGNALS.items():
        if not _is_overridden(handler, method_name):
            continue
        sig = getattr(status, signal_name, None)
        if sig is None:
            continue
        bound = getattr(handler, method_name)
        conn = sig.connect(bound, Qt.QueuedConnection)
        connections.append((sig, conn))
    # Wire device/mount hooks against the Devices object if one exists.
    devices = getattr(handler, "devices", None)
    if devices is not None:
        for method_name, signal_name in _DEVICE_SIGNALS.items():
            if not _is_overridden(handler, method_name):
                continue
            sig = getattr(devices, signal_name, None)
            if sig is None:
                continue
            bound = getattr(handler, method_name)
            conn = sig.connect(bound, Qt.QueuedConnection)
            connections.append((sig, conn))
    return connections
