"""Qt-thread marshalling for Transport callbacks.

`ZmqClientTransport` runs its SUB loop on a background thread. Status,
HalPinHub, and widget slots all live on the Qt main thread and must not
be touched from any other thread. This module bridges the gap.

Two pieces:

* `QtMarshaller(QObject)` — a tiny shim with a single `_invoke(object)`
  signal connected to itself with `Qt.QueuedConnection`. Anything emitted
  from any thread is delivered on the marshaller's owning thread (the
  thread that constructed it — typically the main thread).

* `QtMarshalledTransport(Transport)` — wraps another Transport. Sync
  REQ/REP methods pass straight through (the caller is already on the
  main thread). The `set_on_*` callback registrars wrap each callback
  through the marshaller so the inner transport's background thread can
  fire them safely.

Use it like:

    inner = ZmqClientTransport(endpoint)
    marshaller = QtMarshaller(parent=window)
    transport = QtMarshalledTransport(inner, marshaller)
    transport.start_background_sub_thread()
    ...
    transport.close()
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from qtpy.QtCore import QObject, Qt, Signal

from qtcnc.core.hal_spec import HalPinSpec
from qtcnc.core.state import StateStore
from qtcnc.signals import CommandVerb
from qtcnc.transport.base import (
    ConnectCallback,
    DeclarePinsResult,
    DisconnectCallback,
    ErrorCallback,
    HalPinUpdateCallback,
    LifecycleCallback,
    StateDiffCallback,
    Transport,
)


class QtMarshaller(QObject):
    """Marshal arbitrary callables onto this object's owning Qt thread."""

    _invoke = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._invoke.connect(self._handle, Qt.QueuedConnection)

    def _handle(self, payload: tuple) -> None:
        cb, args, kwargs = payload
        cb(*args, **kwargs)

    def wrap(self, cb: Optional[Callable[..., Any]]) -> Optional[Callable[..., Any]]:
        """Return a thread-safe wrapper that queues `cb(*args, **kwargs)`
        onto this marshaller's owning Qt thread.
        """
        if cb is None:
            return None

        def _wrapped(*args: Any, **kwargs: Any) -> None:
            self._invoke.emit((cb, args, kwargs))

        return _wrapped


class QtMarshalledTransport(Transport):
    """Wrap a Transport so all PUB callbacks fire on the Qt main thread.

    Sync REQ/REP methods pass straight through to the inner transport.
    The `set_on_*` registrars install marshalled wrappers so the inner
    transport's SUB thread never touches Qt state directly.

    `start_background_sub_thread` and `stop_background_sub_thread`
    delegate to the inner transport if it supports them; otherwise
    they're silent no-ops (mock mode case).
    """

    def __init__(self, inner: Transport, marshaller: QtMarshaller) -> None:
        super().__init__()
        self._inner = inner
        self._marshaller = marshaller

    @property
    def inner(self) -> Transport:
        return self._inner

    # ----- callback registration: wrap then forward -----

    def set_on_state_diff(self, cb: StateDiffCallback) -> None:
        self._inner.set_on_state_diff(self._marshaller.wrap(cb))

    def set_on_hal_pin_update(self, cb: HalPinUpdateCallback) -> None:
        self._inner.set_on_hal_pin_update(self._marshaller.wrap(cb))

    def set_on_error(self, cb: ErrorCallback) -> None:
        self._inner.set_on_error(self._marshaller.wrap(cb))

    def set_on_lifecycle(self, cb: LifecycleCallback) -> None:
        self._inner.set_on_lifecycle(self._marshaller.wrap(cb))

    def set_on_connected(self, cb: ConnectCallback) -> None:
        self._inner.set_on_connected(self._marshaller.wrap(cb))

    def set_on_disconnected(self, cb: DisconnectCallback) -> None:
        self._inner.set_on_disconnected(self._marshaller.wrap(cb))

    # ----- sync REQ/REP: passthrough -----

    def hello(self) -> dict[str, Any]:
        return self._inner.hello()

    def get_snapshot(self) -> StateStore:
        return self._inner.get_snapshot()

    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        return self._inner.exec_command(verb, **kwargs)

    def load_program(self, path: str) -> None:
        return self._inner.load_program(path)

    def declare_pins(self, specs: list[HalPinSpec]) -> DeclarePinsResult:
        return self._inner.declare_pins(specs)

    def write_pin(self, name: str, value: Any) -> None:
        return self._inner.write_pin(name, value)

    def subscribe_pin(self, name: str) -> None:
        return self._inner.subscribe_pin(name)

    def ping(self, nonce: int | None = None) -> dict[str, Any]:
        return self._inner.ping(nonce)

    def reopen(self) -> None:
        return self._inner.reopen()

    def close(self) -> None:
        return self._inner.close()

    @property
    def is_connected(self) -> bool:
        return self._inner.is_connected

    # ----- background SUB thread: delegate if supported -----

    def start_background_sub_thread(self, poll_ms: int = 100) -> None:
        start = getattr(self._inner, "start_background_sub_thread", None)
        if start is not None:
            start(poll_ms)

    def stop_background_sub_thread(self) -> None:
        stop = getattr(self._inner, "stop_background_sub_thread", None)
        if stop is not None:
            stop()
