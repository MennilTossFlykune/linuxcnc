"""ZmqServerTransport: daemon-side publisher and REP dispatcher.

Owns a PUB socket (publishes state diffs, HAL pin updates, errors,
lifecycle events) and a REP socket (answers REQs from clients). The
daemon's business logic is not in this file — it's in `server.py` or
whatever uses this module. This file is plumbing: serialization,
dispatch, topic framing.

Endpoint convention: both sockets derive from a single base endpoint
so the client only needs one argument. For `ipc:///tmp/qtcnc-1000`:

* PUB binds to `ipc:///tmp/qtcnc-1000.pub`
* REP binds to `ipc:///tmp/qtcnc-1000.cmd`

For TCP endpoints, use `tcp://host:5570` and the module rewrites to
`tcp://host:5570` (pub) / `tcp://host:5571` (cmd).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

import zmq

from qtcnc.core.hal_spec import HalPinSpec
from qtcnc.core.state import StateStore
from qtcnc.core.types import ErrorMessage
from qtcnc.signals import CommandVerb, MessageType, Topic
from qtcnc.transport.base import DeclarePinsResult, NackError
from qtcnc.transport.codec import (
    UnknownMessageType,
    decode_envelope,
    encode_envelope,
    from_wire,
    to_wire,
)


class ZmqServerHandler(Protocol):
    """The business-logic interface a daemon implements."""

    def on_hello(self) -> dict[str, Any]: ...
    def on_get_snapshot(self) -> StateStore: ...
    def on_exec_command(self, verb: CommandVerb, kwargs: dict[str, Any]) -> None: ...
    def on_load_program(self, path: str) -> None: ...
    def on_declare_pins(self, specs: list[HalPinSpec]) -> DeclarePinsResult: ...
    def on_write_pin(self, name: str, value: Any) -> None: ...
    def on_subscribe_pin(self, name: str) -> None: ...


def _split_endpoint(base: str) -> tuple[str, str]:
    """Return (pub_endpoint, cmd_endpoint) derived from a base endpoint."""
    if base.startswith("ipc://"):
        return base + ".pub", base + ".cmd"
    if base.startswith("tcp://"):
        body = base[len("tcp://") :]
        if ":" not in body:
            raise ValueError(f"tcp endpoint must include a port: {base!r}")
        host, _, port = body.rpartition(":")
        if not port:
            raise ValueError(f"tcp endpoint must include a port: {base!r}")
        try:
            port_int = int(port)
        except ValueError as e:
            raise ValueError(
                f"tcp endpoint port must be an integer: {base!r}",
            ) from e
        return f"tcp://{host}:{port}", f"tcp://{host}:{port_int + 1}"
    if base.startswith("inproc://"):
        return base + ".pub", base + ".cmd"
    raise ValueError(f"unsupported endpoint scheme: {base!r}")


class ZmqServerTransport:
    """Daemon-side ZMQ plumbing: PUB socket + REP dispatcher."""

    def __init__(
        self,
        endpoint: str,
        handler: ZmqServerHandler,
        *,
        context: Optional[zmq.Context] = None,
        sndhwm: int = 10000,
        rcvhwm: int = 10000,
        curve_public_key: Optional[bytes] = None,
        curve_secret_key: Optional[bytes] = None,
        authorized_clients_dir: Optional[str] = None,
    ) -> None:
        self._endpoint = endpoint
        self._handler = handler
        self._ctx = context or zmq.Context.instance()
        self._owns_ctx = context is None
        pub_ep, cmd_ep = _split_endpoint(endpoint)
        self._pub_ep = pub_ep
        self._cmd_ep = cmd_ep
        self._sndhwm = sndhwm
        self._rcvhwm = rcvhwm
        self._state_diff_dropped = 0
        # CURVE auth state. Both keys must be provided together; absent
        # both, the transport runs unencrypted (back-compat).
        if (curve_public_key is None) != (curve_secret_key is None):
            raise ValueError(
                "curve_public_key and curve_secret_key must be set together"
            )
        self._curve_enabled = curve_public_key is not None
        self._curve_public_key = curve_public_key
        self._curve_secret_key = curve_secret_key
        self._authorized_clients_dir = authorized_clients_dir
        self._authenticator = None
        if self._curve_enabled:
            from zmq.auth.thread import ThreadAuthenticator
            self._authenticator = ThreadAuthenticator(self._ctx)
            self._authenticator.start()
            if authorized_clients_dir is not None:
                self._authenticator.configure_curve(
                    domain="*", location=authorized_clients_dir,
                )
            else:
                # No client directory => allow any CURVE client (still
                # encrypted, just unauthenticated).
                self._authenticator.configure_curve(
                    domain="*", location=zmq.auth.CURVE_ALLOW_ANY,
                )
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, sndhwm)
        if self._curve_enabled:
            self._pub.curve_secretkey = curve_secret_key
            self._pub.curve_publickey = curve_public_key
            self._pub.curve_server = True
        self._pub.bind(pub_ep)
        self._rep = self._ctx.socket(zmq.REP)
        self._rep.setsockopt(zmq.RCVHWM, rcvhwm)
        if self._curve_enabled:
            self._rep.curve_secretkey = curve_secret_key
            self._rep.curve_publickey = curve_public_key
            self._rep.curve_server = True
        self._rep.bind(cmd_ep)

    @property
    def curve_enabled(self) -> bool:
        return self._curve_enabled

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
    def state_diff_dropped(self) -> int:
        return self._state_diff_dropped

    # ----- publish side -----

    def _pub_send(self, topic: bytes, data: bytes, *, count_drops: bool = False) -> bool:
        """Non-blocking PUB send. Returns False (and optionally increments
        the drop counter) when ZMQ's outbound HWM is full."""
        try:
            self._pub.send_multipart([topic, data], flags=zmq.NOBLOCK)
            return True
        except zmq.Again:
            if count_drops:
                self._state_diff_dropped += 1
            return False

    def publish_state_diff(self, changes: list[tuple[str, Any]]) -> None:
        data = encode_envelope(
            MessageType.STATE_DIFF,
            {"changes": [(n, to_wire(v)) for (n, v) in changes]},
        )
        self._pub_send(Topic.STATE_DIFF.encode(), data, count_drops=True)

    def publish_state_snapshot(self, snap: StateStore) -> None:
        data = encode_envelope(MessageType.SNAPSHOT, {"snapshot": to_wire(snap)})
        self._pub_send(Topic.STATE_SNAPSHOT.encode(), data)

    def publish_hal_pin(self, name: str, value: Any) -> None:
        data = encode_envelope(
            MessageType.HAL_PIN_UPDATE, {"name": name, "value": to_wire(value)},
        )
        self._pub_send(Topic.HAL_PIN.encode(), data)

    def publish_error(self, err: ErrorMessage) -> None:
        data = encode_envelope(MessageType.ERROR, {"error": to_wire(err)})
        self._pub_send(Topic.ERROR.encode(), data)

    def publish_lifecycle(self, tag: str, payload: dict[str, Any]) -> None:
        data = encode_envelope(
            MessageType.LIFECYCLE,
            {"tag": tag, "payload": to_wire(payload)},
        )
        self._pub_send(Topic.LIFECYCLE.encode(), data)

    # ----- REP side -----

    def serve_once(self, timeout_ms: int = -1) -> bool:
        """Handle one REQ if available. Returns True if a message was served.

        `timeout_ms`:
        - -1 blocks until a message arrives
        - 0 returns immediately if nothing is pending
        - N waits up to N ms
        """
        poller = zmq.Poller()
        poller.register(self._rep, zmq.POLLIN)
        events = dict(poller.poll(timeout_ms))
        if self._rep not in events:
            return False
        raw = self._rep.recv()
        try:
            env = decode_envelope(raw)
        except Exception as e:
            self._rep.send(encode_envelope(MessageType.NACK, {"reason": f"bad envelope: {e}"}))
            return True
        reply = self._dispatch(env.type, env.payload)
        self._rep.send(reply)
        return True

    def _dispatch(self, msg_type: MessageType, payload: dict[str, Any]) -> bytes:
        try:
            if msg_type == MessageType.HELLO:
                reply_payload = self._handler.on_hello()
                return encode_envelope(MessageType.WELCOME, to_wire(reply_payload))
            if msg_type == MessageType.PING:
                # Echo any nonce the client sent so a Reconnector can
                # tie a PONG back to the PING that triggered it.
                nonce = payload.get("nonce")
                pong_payload: dict[str, Any] = {}
                if nonce is not None:
                    pong_payload["nonce"] = nonce
                return encode_envelope(MessageType.PONG, pong_payload)
            if msg_type == MessageType.GET_SNAPSHOT:
                snap = self._handler.on_get_snapshot()
                return encode_envelope(
                    MessageType.SNAPSHOT, {"snapshot": to_wire(snap)},
                )
            if msg_type == MessageType.EXEC_COMMAND:
                verb_str = payload.get("verb")
                kwargs = payload.get("kwargs", {}) or {}
                try:
                    verb = CommandVerb(verb_str)
                except ValueError:
                    return encode_envelope(
                        MessageType.NACK, {"reason": f"unknown verb: {verb_str}"},
                    )
                # Decode TaskMode argument if present.
                if "mode" in kwargs and isinstance(kwargs["mode"], int):
                    from qtcnc.core.types import TaskMode
                    kwargs["mode"] = TaskMode(kwargs["mode"])
                self._handler.on_exec_command(verb, kwargs)
                return encode_envelope(MessageType.ACK, {})
            if msg_type == MessageType.LOAD_PROGRAM:
                self._handler.on_load_program(payload.get("path", ""))
                return encode_envelope(MessageType.ACK, {})
            if msg_type == MessageType.DECLARE_PINS:
                from typing import List
                specs = from_wire(payload.get("specs", []), List[HalPinSpec])
                result = self._handler.on_declare_pins(specs)
                return encode_envelope(
                    MessageType.ACK,
                    {
                        "created": list(result.created),
                        "inherited": bool(result.inherited),
                    },
                )
            if msg_type == MessageType.WRITE_PIN:
                self._handler.on_write_pin(payload["name"], payload["value"])
                return encode_envelope(MessageType.ACK, {})
            if msg_type == MessageType.SUBSCRIBE_PIN:
                self._handler.on_subscribe_pin(payload["name"])
                return encode_envelope(MessageType.ACK, {})
            if msg_type == MessageType.BYE:
                return encode_envelope(MessageType.ACK, {})
            return encode_envelope(
                MessageType.NACK, {"reason": f"unhandled type: {msg_type}"},
            )
        except NackError as e:
            return encode_envelope(MessageType.NACK, {"reason": e.reason})
        except Exception as e:  # defensive — never crash the REP loop
            return encode_envelope(MessageType.NACK, {"reason": f"server error: {e}"})

    # ----- teardown -----

    def close(self) -> None:
        try:
            self._pub.close(linger=0)
        except Exception:
            pass
        try:
            self._rep.close(linger=0)
        except Exception:
            pass
        if self._authenticator is not None:
            try:
                self._authenticator.stop()
            except Exception:
                pass
            self._authenticator = None
        if self._owns_ctx:
            try:
                self._ctx.term()
            except Exception:
                pass
