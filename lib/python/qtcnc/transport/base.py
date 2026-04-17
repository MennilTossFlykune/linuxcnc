"""Transport abstract base class.

The Transport is the wire-level interface between the GUI client and the
daemon. It has two halves:

* **Sync REQ/REP**: hello, get_snapshot, exec_command, load_program,
  declare_pins, write_pin, subscribe_pin, close. Each returns the typed
  result or raises TransportError on failure.

* **Async PUB/SUB callbacks**: on_state_diff, on_hal_pin_update, on_error,
  on_lifecycle, on_connected, on_disconnected. The Transport invokes these
  when PUB messages arrive. ZmqClientTransport dispatches them from a
  background thread; MockTransport invokes them synchronously. Either way
  the client (Status) is responsible for marshalling into Qt via
  Qt.QueuedConnection when needed.

No Qt imports here; Transport is pure-Python so it can be unit-tested
without a QApplication.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from qtcnc.core.hal_spec import HalPinSpec
from qtcnc.core.state import StateStore
from qtcnc.core.types import ErrorMessage, GetToolDbResult
from qtcnc.signals import CommandVerb


@dataclass(frozen=True, slots=True)
class DeclarePinsResult:
    """Result of a DECLARE_PINS request.

    `created` is the list of pin names that now exist on the daemon for
    this client to read/write. `inherited` is True when the pins were
    already declared by an earlier client — the daemon confirmed they
    match (same type/dir) and this client is joining an existing
    session, not creating new HAL surface.
    """

    created: list[str] = field(default_factory=list)
    inherited: bool = False


StateDiffCallback = Callable[[list[tuple[str, Any]]], None]
HalPinUpdateCallback = Callable[[str, Any], None]
ErrorCallback = Callable[[ErrorMessage], None]
LifecycleCallback = Callable[[str, dict[str, Any]], None]
ConnectCallback = Callable[[], None]
DisconnectCallback = Callable[[str], None]


class TransportError(Exception):
    """Base class for all transport failures."""


class NackError(TransportError):
    """A REQ was acknowledged with a NACK by the peer."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class TransportClosed(TransportError):
    """The transport was closed while a request was in flight."""


class ProtocolVersionMismatch(TransportError):
    """Daemon and client disagree on protocol major version."""

    def __init__(self, client_version: tuple[int, int], daemon_version: tuple[int, int]):
        super().__init__(
            f"protocol mismatch: client {client_version} vs daemon {daemon_version}"
        )
        self.client_version = client_version
        self.daemon_version = daemon_version


class Transport(ABC):
    """Abstract transport interface."""

    def __init__(self) -> None:
        self._on_state_diff: Optional[StateDiffCallback] = None
        self._on_hal_pin_update: Optional[HalPinUpdateCallback] = None
        self._on_error: Optional[ErrorCallback] = None
        self._on_lifecycle: Optional[LifecycleCallback] = None
        self._on_connected: Optional[ConnectCallback] = None
        self._on_disconnected: Optional[DisconnectCallback] = None

    # --- callback registration ---

    def set_on_state_diff(self, cb: StateDiffCallback) -> None:
        self._on_state_diff = cb

    def set_on_hal_pin_update(self, cb: HalPinUpdateCallback) -> None:
        self._on_hal_pin_update = cb

    def set_on_error(self, cb: ErrorCallback) -> None:
        self._on_error = cb

    def set_on_lifecycle(self, cb: LifecycleCallback) -> None:
        self._on_lifecycle = cb

    def set_on_connected(self, cb: ConnectCallback) -> None:
        self._on_connected = cb

    def set_on_disconnected(self, cb: DisconnectCallback) -> None:
        self._on_disconnected = cb

    # --- dispatch helpers for subclasses ---

    def _dispatch_state_diff(self, changes: list[tuple[str, Any]]) -> None:
        if self._on_state_diff is not None:
            self._on_state_diff(changes)

    def _dispatch_hal_pin_update(self, name: str, value: Any) -> None:
        if self._on_hal_pin_update is not None:
            self._on_hal_pin_update(name, value)

    def _dispatch_error(self, err: ErrorMessage) -> None:
        if self._on_error is not None:
            self._on_error(err)

    def _dispatch_lifecycle(self, tag: str, payload: dict[str, Any]) -> None:
        if self._on_lifecycle is not None:
            self._on_lifecycle(tag, payload)

    def _dispatch_connected(self) -> None:
        if self._on_connected is not None:
            self._on_connected()

    def _dispatch_disconnected(self, reason: str) -> None:
        if self._on_disconnected is not None:
            self._on_disconnected(reason)

    # --- sync REQ/REP interface ---

    @abstractmethod
    def hello(self) -> dict[str, Any]:
        """Negotiate protocol version. Returns the WELCOME payload."""

    @abstractmethod
    def get_snapshot(self) -> StateStore:
        """Fetch the full current state."""

    @abstractmethod
    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        """Send a command. Raises NackError on failure."""

    @abstractmethod
    def load_program(self, path: str) -> None:
        """Ask the daemon to load a g-code file."""

    @abstractmethod
    def declare_pins(self, specs: list[HalPinSpec]) -> DeclarePinsResult:
        """Declare HAL pins.

        Returns a `DeclarePinsResult` with the names that now exist on
        the daemon and an `inherited` flag indicating whether this call
        joined an existing HAL component (True) or created it (False).

        Raises NackError with reason `"hal_locked"` when the daemon has
        already locked HAL and the requested specs are not a subset of
        what already exists, or `"pin_type_mismatch"` when a requested
        name exists with a different type or direction.
        """

    @abstractmethod
    def write_pin(self, name: str, value: Any) -> None:
        """Write a value to a HAL pin owned by the qtcnc component."""

    @abstractmethod
    def subscribe_pin(self, name: str) -> None:
        """Ask the daemon to start publishing updates for a foreign pin."""

    @abstractmethod
    def close(self) -> None:
        """Tear down sockets/threads. Idempotent."""

    # --- tool database ---

    @abstractmethod
    def get_tool_db(self) -> GetToolDbResult:
        """Fetch the full tool database from the daemon."""

    @abstractmethod
    def add_tool(self, tool_id: int, pocket: int, **fields: Any) -> None:
        """Add a tool to the database. Raises NackError on failure."""

    @abstractmethod
    def remove_tool(self, tool_id: int) -> None:
        """Remove a tool from the database. Raises NackError on failure."""

    @abstractmethod
    def update_tool(self, tool_id: int, **fields: Any) -> None:
        """Update a tool's fields. Raises NackError on failure."""

    # --- liveness + reopen (used by Reconnector) ---

    def ping(self, nonce: int | None = None) -> dict[str, Any]:
        """Send a liveness check. Default: raise NotImplementedError.

        Subclasses that need the Reconnector must override this. The
        return value is the PONG payload (so callers can match `nonce`).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement ping()"
        )

    def reopen(self) -> None:
        """Tear down + re-create the underlying sockets without dropping
        the Transport instance. Default is a no-op so MockTransport
        (which has no sockets) "supports" reopen trivially."""
        return None

    # --- convenience ---

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...
