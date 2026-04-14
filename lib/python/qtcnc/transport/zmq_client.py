"""ZmqClientTransport: client-side REQ + SUB transport.

This is the implementation of the `Transport` ABC for a live GUI client
talking to a running `qtcnc-serverd`. It owns two ZMQ sockets:

* **REQ** (connects to `<base>.cmd`) — synchronous requests: `HELLO`,
  `GET_SNAPSHOT`, `EXEC_COMMAND`, `LOAD_PROGRAM`, `DECLARE_PINS`,
  `WRITE_PIN`, `SUBSCRIBE_PIN`, `BYE`. Every call blocks until a reply
  arrives or the per-request timeout fires. A `NACK` reply is raised as
  `NackError`.

* **SUB** (connects to `<base>.pub`) — asynchronous topic stream:
  `state.diff`, `state.snapshot`, `hal.pin`, `error`, `lifecycle`. The
  client drives this via `pump_sub(timeout_ms)` which polls, decodes
  envelopes, and fans out to the registered callbacks.

Two delivery modes for `pump_sub`:

1. **Synchronous**: tests call `pump_sub(timeout_ms)` directly from the
   main thread. Deterministic, no thread marshalling.
2. **Background thread**: `start_background_sub_thread()` spawns a daemon
   thread that loops on `pump_sub(100)` forever. Callbacks then fire from
   the background thread, and the Qt client (Status) marshals into the
   GUI thread via `Qt.QueuedConnection` — the existing pattern.

Design notes:

* The REQ socket is guarded by an RLock so any thread may call a sync
  method (including the SUB pump thread itself, though it currently
  doesn't). The SUB socket is unguarded: only `pump_sub` touches it and
  only one thread at a time is expected to drive pumping.
* On `hello()` the transport fires `_dispatch_connected()`. On `close()`
  it fires `_dispatch_disconnected("closed")` if the hello had succeeded.
* Context ownership: if the caller passes a `zmq.Context`, this class
  does NOT terminate it on close. If no context is passed, a fresh
  private `zmq.Context()` is created and terminated on close. (We never
  touch `zmq.Context.instance()` — terminating a shared global would
  break unrelated code.)
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import fields
from typing import Any, Optional

import zmq

from qtcnc import PROTOCOL_VERSION
from qtcnc.core.hal_spec import HalPinSpec
from qtcnc.core.state import StateStore
from qtcnc.core.types import ErrorMessage
from qtcnc.signals import CommandVerb, MessageType, Topic
from qtcnc.transport.base import (
    DeclarePinsResult,
    NackError,
    ProtocolVersionMismatch,
    Transport,
    TransportClosed,
    TransportError,
)
from qtcnc.transport.codec import (
    _resolved_hints,
    decode_envelope,
    encode_envelope,
    from_wire,
    to_wire,
    version_compatible,
)
from qtcnc.transport.zmq_server import _split_endpoint


_SUBSCRIBED_TOPICS: tuple[Topic, ...] = (
    Topic.STATE_DIFF,
    Topic.STATE_SNAPSHOT,
    Topic.HAL_PIN,
    Topic.ERROR,
    Topic.LIFECYCLE,
)


class ZmqClientTransport(Transport):
    """ZMQ client transport speaking the qtcnc wire protocol."""

    def __init__(
        self,
        endpoint: str,
        *,
        context: Optional[zmq.Context] = None,
        client_id: Optional[str] = None,
        request_timeout_ms: int = 5000,
        rcvhwm: int = 100000,
        curve_server_key: Optional[bytes] = None,
        curve_public_key: Optional[bytes] = None,
        curve_secret_key: Optional[bytes] = None,
        tcp_keepalive: Optional[bool] = None,
    ) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._ctx = context if context is not None else zmq.Context()
        self._owns_ctx = context is None
        pub_ep, cmd_ep = _split_endpoint(endpoint)
        self._pub_ep = pub_ep
        self._cmd_ep = cmd_ep
        self._client_id = client_id or str(uuid.uuid4())
        self._next_req_id = 0
        self._request_timeout_ms = request_timeout_ms
        self._rcvhwm = rcvhwm
        self._closed = False
        self._connected = False
        # CURVE: all three keys must travel together. We don't enable a
        # half-configured CURVE because the resulting state is silent
        # protocol failure (the handshake never completes).
        curve_args = (curve_server_key, curve_public_key, curve_secret_key)
        if any(k is not None for k in curve_args) and not all(curve_args):
            raise ValueError(
                "curve_server_key, curve_public_key, and curve_secret_key "
                "must all be set together"
            )
        self._curve_enabled = curve_server_key is not None
        self._curve_server_key = curve_server_key
        self._curve_public_key = curve_public_key
        self._curve_secret_key = curve_secret_key
        # Default TCP keepalive: enabled for tcp:// (so dead links surface
        # as zmq errors instead of silently stalling), disabled for ipc.
        if tcp_keepalive is None:
            tcp_keepalive = endpoint.startswith("tcp://")
        self._tcp_keepalive = tcp_keepalive
        # StateStore field -> type annotation, used to decode diff values.
        self._state_hints = _resolved_hints(StateStore)
        self._lock = threading.RLock()
        self._sub_thread: Optional[threading.Thread] = None
        self._stop_sub = threading.Event()
        self._req: Optional[zmq.Socket] = None
        self._sub: Optional[zmq.Socket] = None
        # The ZMQ REQ socket is single-shot per request: if a recv() times
        # out, the REQ state machine is stuck and the socket must be
        # recreated. We set this flag on timeout and refuse subsequent
        # requests until reopen() clears it.
        self._req_poisoned: bool = False
        self._open_sockets()

    @property
    def curve_enabled(self) -> bool:
        return self._curve_enabled

    # ----- socket lifecycle -----

    def _apply_curve_client(self, sock: zmq.Socket) -> None:
        sock.curve_serverkey = self._curve_server_key
        sock.curve_publickey = self._curve_public_key
        sock.curve_secretkey = self._curve_secret_key

    def _apply_keepalive(self, sock: zmq.Socket) -> None:
        if self._tcp_keepalive:
            sock.setsockopt(zmq.TCP_KEEPALIVE, 1)
            sock.setsockopt(zmq.TCP_KEEPALIVE_IDLE, 30)
            sock.setsockopt(zmq.TCP_KEEPALIVE_INTVL, 5)
            sock.setsockopt(zmq.TCP_KEEPALIVE_CNT, 3)

    def _open_sockets(self) -> None:
        """Create + connect REQ and SUB sockets. Idempotent on already-open
        instances: caller must close first."""
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.setsockopt(zmq.RCVTIMEO, self._request_timeout_ms)
        self._req.setsockopt(zmq.SNDTIMEO, self._request_timeout_ms)
        self._apply_keepalive(self._req)
        if self._curve_enabled:
            self._apply_curve_client(self._req)
        self._req.connect(self._cmd_ep)

        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.setsockopt(zmq.LINGER, 0)
        self._sub.setsockopt(zmq.RCVHWM, self._rcvhwm)
        self._apply_keepalive(self._sub)
        if self._curve_enabled:
            self._apply_curve_client(self._sub)
        self._sub.connect(self._pub_ep)
        for topic in _SUBSCRIBED_TOPICS:
            self._sub.setsockopt(zmq.SUBSCRIBE, topic.encode())

    def _close_sockets(self) -> None:
        """Tear down REQ and SUB without touching the context. Used by
        both `close()` and `reopen()`."""
        if self._req is not None:
            try:
                self._req.close(linger=0)
            except Exception:
                pass
            self._req = None
        if self._sub is not None:
            try:
                self._sub.close(linger=0)
            except Exception:
                pass
            self._sub = None

    def reopen(self) -> None:
        """Tear down REQ + SUB sockets and create fresh ones.

        Used by `Reconnector` after a connection drop: a REQ socket that
        timed out is in an unusable state (the ZMQ REQ state machine
        requires a recv per send), so we must throw it away and start
        over rather than trying to recover it. The next `hello()` call
        will trigger `_dispatch_connected()` if it succeeds.
        """
        with self._lock:
            if self._closed:
                raise TransportClosed("client transport is closed")
            self._connected = False
            self._req_poisoned = False
            self._close_sockets()
            self._open_sockets()
            # Stop the existing background SUB thread (if any) since it
            # holds a reference to the now-defunct socket. The caller is
            # expected to restart it.
        if self._sub_thread is not None:
            self.stop_background_sub_thread()

    # ----- properties -----

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def pub_endpoint(self) -> str:
        return self._pub_ep

    @property
    def cmd_endpoint(self) -> str:
        return self._cmd_ep

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._closed

    # ----- low-level request helper -----

    def _allocate_id(self) -> int:
        with self._lock:
            self._next_req_id += 1
            return self._next_req_id

    def _request(
        self,
        msg_type: MessageType,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a REQ envelope and return the REP payload.

        Raises:
            TransportClosed: if the transport has been closed.
            NackError: if the peer replied with NACK.
            TransportError: on socket errors or timeouts.
        """
        with self._lock:
            if self._closed:
                raise TransportClosed("client transport is closed")
            if self._req_poisoned or self._req is None:
                raise TransportError(
                    f"request {msg_type} aborted: REQ socket needs reopen()"
                )
            req_id = self._allocate_id()
            data = encode_envelope(
                msg_type, payload or {}, id=req_id, client=self._client_id,
            )
            try:
                self._req.send(data)
                raw = self._req.recv()
            except zmq.Again as e:
                # REQ state machine is now stuck mid-cycle; mark the
                # socket as needing recreation before raising.
                self._req_poisoned = True
                raise TransportError(f"request {msg_type} timed out: {e}") from e
            except zmq.ZMQError as e:
                self._req_poisoned = True
                raise TransportError(f"request {msg_type} failed: {e}") from e
        env = decode_envelope(raw)
        if env.type == MessageType.NACK:
            raise NackError(str(env.payload.get("reason", "nack")))
        return env.payload

    # ----- Transport ABC: sync REQ/REP -----

    def hello(self) -> dict[str, Any]:
        reply = self._request(MessageType.HELLO, {})
        daemon_v = reply.get("protocol_version")
        if isinstance(daemon_v, (list, tuple)) and len(daemon_v) == 2:
            dv = (int(daemon_v[0]), int(daemon_v[1]))
            if not version_compatible(PROTOCOL_VERSION, dv):
                raise ProtocolVersionMismatch(PROTOCOL_VERSION, dv)
        self._connected = True
        self._dispatch_connected()
        return reply

    def get_snapshot(self) -> StateStore:
        reply = self._request(MessageType.GET_SNAPSHOT, {})
        snap = reply.get("snapshot")
        if not isinstance(snap, dict):
            raise TransportError(f"snapshot payload malformed: {snap!r}")
        return from_wire(snap, StateStore)

    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        payload = {"verb": str(verb), "kwargs": to_wire(kwargs)}
        self._request(MessageType.EXEC_COMMAND, payload)

    def load_program(self, path: str) -> None:
        self._request(MessageType.LOAD_PROGRAM, {"path": path})

    def declare_pins(self, specs: list[HalPinSpec]) -> DeclarePinsResult:
        reply = self._request(
            MessageType.DECLARE_PINS, {"specs": to_wire(specs)},
        )
        created = reply.get("created", [])
        if not isinstance(created, list):
            raise TransportError(f"declare_pins reply malformed: {reply!r}")
        return DeclarePinsResult(
            created=[str(c) for c in created],
            inherited=bool(reply.get("inherited", False)),
        )

    def write_pin(self, name: str, value: Any) -> None:
        self._request(
            MessageType.WRITE_PIN, {"name": name, "value": to_wire(value)},
        )

    def subscribe_pin(self, name: str) -> None:
        self._request(MessageType.SUBSCRIBE_PIN, {"name": name})

    def ping(self, nonce: int | None = None) -> dict[str, Any]:
        """Send a PING and return the PONG payload.

        If `nonce` is provided, the daemon echoes it back; the caller
        can match up the reply to the request. The Reconnector uses this
        to confirm liveness without having to trust REQ ordering across
        a stalled socket.
        """
        payload: dict[str, Any] = {}
        if nonce is not None:
            payload["nonce"] = nonce
        return self._request(MessageType.PING, payload)

    def close(self) -> None:
        # Stop background SUB pumping before tearing down the socket.
        self.stop_background_sub_thread()
        with self._lock:
            if self._closed:
                return
            was_connected = self._connected
            if was_connected:
                try:
                    self._request(MessageType.BYE, {})
                except Exception:
                    pass  # best effort
            self._closed = True
            self._connected = False
            self._close_sockets()
            if self._owns_ctx:
                try:
                    self._ctx.term()
                except Exception:
                    pass
        if was_connected:
            self._dispatch_disconnected("closed")

    # ----- SUB pump -----

    def pump_sub(self, timeout_ms: int = 0) -> int:
        """Drain any pending PUB messages, dispatching callbacks.

        `timeout_ms` is the time to wait for the first message:
        - 0 returns immediately if nothing is queued
        - -1 blocks until a message arrives
        - N waits up to N ms

        After the first message is drained, any remaining queued
        messages are also consumed non-blocking. Returns the number of
        messages dispatched.
        """
        if self._closed:
            return 0
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        try:
            events = dict(poller.poll(timeout_ms))
        except zmq.ZMQError:
            return 0
        dispatched = 0
        while self._sub in events:
            try:
                parts = self._sub.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
            except zmq.ZMQError:
                break
            if len(parts) != 2:
                # malformed topic frame; skip silently
                continue
            _, body = parts
            try:
                self._handle_pub_message(body)
                dispatched += 1
            except Exception:
                # Never let a single bad message kill the pump.
                continue
            try:
                events = dict(poller.poll(0))
            except zmq.ZMQError:
                break
        return dispatched

    def _handle_pub_message(self, body: bytes) -> None:
        env = decode_envelope(body)
        msg = env.type
        payload = env.payload
        if msg == MessageType.STATE_DIFF:
            raw_changes = payload.get("changes", [])
            changes = self._decode_state_diff(raw_changes)
            self._dispatch_state_diff(changes)
            return
        if msg == MessageType.SNAPSHOT:
            snap_wire = payload.get("snapshot")
            if isinstance(snap_wire, dict):
                state = from_wire(snap_wire, StateStore)
                full = [(f.name, getattr(state, f.name)) for f in fields(StateStore)]
                self._dispatch_state_diff(full)
            return
        if msg == MessageType.HAL_PIN_UPDATE:
            name = payload.get("name", "")
            value = payload.get("value")
            if isinstance(name, str):
                self._dispatch_hal_pin_update(name, value)
            return
        if msg == MessageType.ERROR:
            err_wire = payload.get("error")
            if isinstance(err_wire, dict):
                err = from_wire(err_wire, ErrorMessage)
                self._dispatch_error(err)
            return
        if msg == MessageType.LIFECYCLE:
            tag = payload.get("tag", "")
            inner = payload.get("payload", {}) or {}
            if isinstance(tag, str) and isinstance(inner, dict):
                self._dispatch_lifecycle(tag, inner)
            return
        # Unknown message types on PUB are ignored — forward-compat.

    def _decode_state_diff(
        self, raw_changes: list[Any],
    ) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for item in raw_changes:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            name, wire_value = item[0], item[1]
            if not isinstance(name, str):
                continue
            target = self._state_hints.get(name, Any)
            try:
                value = from_wire(wire_value, target)
            except Exception:
                value = wire_value
            out.append((name, value))
        return out

    # ----- optional background SUB thread -----

    def start_background_sub_thread(self, poll_ms: int = 100) -> None:
        """Spawn a daemon thread that loops on `pump_sub(poll_ms)`.

        Callbacks then fire from the background thread — the GUI client
        is responsible for marshalling into the Qt main thread via
        `Qt.QueuedConnection`.
        """
        if self._sub_thread is not None or self._closed:
            return
        self._stop_sub.clear()

        def _run() -> None:
            while not self._stop_sub.is_set():
                try:
                    self.pump_sub(poll_ms)
                except Exception:
                    break

        self._sub_thread = threading.Thread(
            target=_run, daemon=True, name="qtcnc-sub",
        )
        self._sub_thread.start()

    def stop_background_sub_thread(self) -> None:
        if self._sub_thread is None:
            return
        self._stop_sub.set()
        self._sub_thread.join(timeout=2.0)
        self._sub_thread = None
