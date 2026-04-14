"""Tests for qtcnc.transport.base — callback dispatch and error types."""

from __future__ import annotations

from typing import Any

import pytest

from qtcnc.core.hal_spec import HalPinSpec
from qtcnc.core.state import StateStore
from qtcnc.core.types import ErrorMessage, ErrorSeverity
from qtcnc.signals import CommandVerb
from qtcnc.transport.base import (
    DeclarePinsResult,
    NackError,
    ProtocolVersionMismatch,
    Transport,
    TransportClosed,
    TransportError,
)


class _StubTransport(Transport):
    """Minimal Transport subclass that exposes the dispatch helpers."""

    def __init__(self) -> None:
        super().__init__()
        self._closed = False

    def hello(self) -> dict[str, Any]:
        return {"protocol_version": (1, 0)}

    def get_snapshot(self) -> StateStore:
        return StateStore()

    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        return None

    def load_program(self, path: str) -> None:
        return None

    def declare_pins(self, specs: list[HalPinSpec]) -> DeclarePinsResult:
        return DeclarePinsResult(created=[s.name for s in specs], inherited=False)

    def write_pin(self, name: str, value: Any) -> None:
        return None

    def subscribe_pin(self, name: str) -> None:
        return None

    def close(self) -> None:
        self._closed = True

    @property
    def is_connected(self) -> bool:
        return not self._closed


class TestDispatch:
    def test_state_diff_callback(self):
        t = _StubTransport()
        seen: list[list[tuple[str, Any]]] = []
        t.set_on_state_diff(lambda changes: seen.append(changes))
        t._dispatch_state_diff([("feed_rate", 1.0)])
        assert seen == [[("feed_rate", 1.0)]]

    def test_no_callback_is_silent(self):
        t = _StubTransport()
        t._dispatch_state_diff([("feed_rate", 1.0)])  # no crash
        t._dispatch_hal_pin_update("p", 1)
        t._dispatch_error(ErrorMessage(ErrorSeverity.INFO, "hi", 0.0))
        t._dispatch_lifecycle("tag", {})
        t._dispatch_connected()
        t._dispatch_disconnected("test")

    def test_hal_pin_update_callback(self):
        t = _StubTransport()
        seen: list[tuple[str, Any]] = []
        t.set_on_hal_pin_update(lambda name, value: seen.append((name, value)))
        t._dispatch_hal_pin_update("qtcnc.foo", 42.0)
        assert seen == [("qtcnc.foo", 42.0)]

    def test_error_callback(self):
        t = _StubTransport()
        seen: list[ErrorMessage] = []
        t.set_on_error(seen.append)
        err = ErrorMessage(ErrorSeverity.OPERATOR_ERROR, "oops", 1.0)
        t._dispatch_error(err)
        assert seen == [err]

    def test_lifecycle_callback(self):
        t = _StubTransport()
        seen: list[tuple[str, dict]] = []
        t.set_on_lifecycle(lambda tag, payload: seen.append((tag, payload)))
        t._dispatch_lifecycle("program_loaded", {"path": "/f.ngc"})
        assert seen == [("program_loaded", {"path": "/f.ngc"})]

    def test_connect_disconnect_callbacks(self):
        t = _StubTransport()
        events: list[str] = []
        t.set_on_connected(lambda: events.append("up"))
        t.set_on_disconnected(lambda reason: events.append(f"down:{reason}"))
        t._dispatch_connected()
        t._dispatch_disconnected("timeout")
        assert events == ["up", "down:timeout"]


class TestErrors:
    def test_nack_has_reason(self):
        e = NackError("hal_locked")
        assert e.reason == "hal_locked"
        assert isinstance(e, TransportError)

    def test_version_mismatch(self):
        e = ProtocolVersionMismatch((1, 0), (2, 0))
        assert e.client_version == (1, 0)
        assert e.daemon_version == (2, 0)
        assert isinstance(e, TransportError)

    def test_transport_closed_is_transport_error(self):
        assert issubclass(TransportClosed, TransportError)


class TestAbstractMethods:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            Transport()  # type: ignore[abstract]

    def test_stub_works(self):
        t = _StubTransport()
        assert t.is_connected is True
        t.close()
        assert t.is_connected is False
