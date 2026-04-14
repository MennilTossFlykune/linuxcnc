"""Reconnector: PING liveness loop + automatic reconnect with state replay.

The Reconnector is the piece that turns a transient daemon failure into
a recoverable event instead of a dead client. Three concerns:

1. **Liveness**: a `QTimer` fires `transport.ping(nonce)` at `ping_hz`.
   Three consecutive failures (configurable via `miss_threshold`) flip the
   state machine to DISCONNECTED.

2. **Reconnect**: from DISCONNECTED, schedule `_try_reconnect` after a
   backoff (1s, 2s, 4s, … capped at 10s). Reconnect calls
   `transport.reopen() → hello() → Status.bootstrap() → replay
   subscribe_pin`. On success the state machine returns to CONNECTED and
   the ping loop resumes. On failure the backoff doubles and we try again.

3. **Daemon restart detection**: every `WELCOME` carries a
   `daemon_instance_id`. If the id seen on a successful reconnect differs
   from the last one, the daemon was restarted (rather than the network
   merely being interrupted). The Reconnector emits `daemon_restarted` so
   the handler can take corrective action — most importantly, re-declare
   HAL pins, since the new daemon process owns a fresh HAL component.

Threading model: everything runs on the thread the Reconnector was
created on (typically the Qt main thread). `QTimer` does the scheduling;
no background threads. Transport calls (`ping`, `reopen`, `hello`,
`get_snapshot`) are blocking, but on a healthy local IPC link they
return in microseconds. On TCP they could block the GUI for a round
trip — documented risk in the v2 plan, revisit if it bites.

The Reconnector does NOT directly fire `Status.connected/disconnected`.
It emits `state_changed` and `Status.attach_reconnector(self)` wires the
translation. This keeps Status free of any dependency on Reconnector and
keeps the Reconnector free of any dependency on Status's internals.
"""

from __future__ import annotations

from typing import Optional

from qtpy.QtCore import QObject, QTimer, Signal

from qtcnc.transport.base import Transport, TransportClosed


# State labels, also exposed via state_changed.
STATE_CONNECTED = "CONNECTED"
STATE_DISCONNECTED = "DISCONNECTED"
STATE_RECONNECTING = "RECONNECTING"


class Reconnector(QObject):
    """PING-liveness state machine that recovers a Transport after a drop."""

    state_changed = Signal(str)
    reconnect_attempt = Signal(int)
    daemon_restarted = Signal()

    def __init__(
        self,
        transport: Transport,
        status: "Optional[object]" = None,
        *,
        ping_hz: float = 1.0,
        miss_threshold: int = 3,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 10.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if ping_hz <= 0:
            raise ValueError(f"ping_hz must be > 0, got {ping_hz}")
        if miss_threshold < 1:
            raise ValueError(f"miss_threshold must be >= 1, got {miss_threshold}")
        self._transport = transport
        self._status = status
        self._ping_period_ms = max(1, int(round(1000.0 / ping_hz)))
        self._miss_threshold = miss_threshold
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._missed = 0
        self._next_nonce = 0
        self._state = STATE_CONNECTED
        self._backoff_s = backoff_initial_s
        self._attempt = 0
        self._daemon_instance_id: Optional[str] = None
        self._subscribed_pins: set[str] = set()
        self._stopped = False

        self._ping_timer = QTimer(self)
        self._ping_timer.setInterval(self._ping_period_ms)
        self._ping_timer.timeout.connect(self._on_ping_timer)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._try_reconnect)

    # ----- public API -----

    @property
    def state(self) -> str:
        return self._state

    @property
    def missed(self) -> int:
        return self._missed

    @property
    def daemon_instance_id(self) -> Optional[str]:
        return self._daemon_instance_id

    @property
    def subscribed_pins(self) -> frozenset[str]:
        return frozenset(self._subscribed_pins)

    def record_initial_id(self, daemon_instance_id: Optional[str]) -> None:
        """Seed the daemon-instance-id tracker from the first WELCOME.

        Bootstrap calls this after `transport.hello()` returns so the
        first reconnect can compare against a real id (and not fire
        `daemon_restarted` against a None baseline).
        """
        self._daemon_instance_id = daemon_instance_id

    def track_pin(self, name: str) -> None:
        """Record a foreign pin that should be re-subscribed on reconnect.

        Widgets that subscribe to non-owned HAL pins via
        `Command.subscribe_pin` should also call this so the Reconnector
        replays the subscription after a daemon restart.
        """
        self._subscribed_pins.add(name)

    def forget_pin(self, name: str) -> None:
        self._subscribed_pins.discard(name)

    def start(self) -> None:
        """Begin the PING loop."""
        self._stopped = False
        self._ping_timer.start()

    def stop(self) -> None:
        """Stop both timers; safe to call multiple times."""
        self._stopped = True
        self._ping_timer.stop()
        self._reconnect_timer.stop()

    # ----- ping loop -----

    def _set_state(self, new_state: str) -> None:
        if self._state != new_state:
            self._state = new_state
            self.state_changed.emit(new_state)

    def _on_ping_timer(self) -> None:
        if self._stopped:
            return
        if self._state in (STATE_DISCONNECTED, STATE_RECONNECTING):
            return
        self._next_nonce += 1
        nonce = self._next_nonce
        try:
            reply = self._transport.ping(nonce=nonce)
        except TransportClosed:
            # Closed transports never recover; stop probing.
            self.stop()
            return
        except Exception:
            self._missed += 1
            if self._missed >= self._miss_threshold:
                self._handle_disconnect("ping_failed")
            return
        # Optional nonce check: if the reply carries a nonce it must match.
        # Mismatched/missing nonce is treated as a successful PONG to stay
        # tolerant of older daemons that don't echo it.
        if isinstance(reply, dict):
            echoed = reply.get("nonce")
            if echoed is not None and echoed != nonce:
                self._missed += 1
                if self._missed >= self._miss_threshold:
                    self._handle_disconnect("ping_nonce_mismatch")
                return
        self._missed = 0
        # If we were probing after a partial success keep state CONNECTED;
        # _set_state is idempotent.
        self._set_state(STATE_CONNECTED)

    # ----- reconnect loop -----

    def _handle_disconnect(self, reason: str) -> None:
        self._ping_timer.stop()
        self._set_state(STATE_DISCONNECTED)
        self._backoff_s = self._backoff_initial_s
        self._attempt = 0
        self._reconnect_timer.start(int(self._backoff_s * 1000))

    def _try_reconnect(self) -> None:
        if self._stopped:
            return
        self._attempt += 1
        self._set_state(STATE_RECONNECTING)
        self.reconnect_attempt.emit(self._attempt)
        try:
            self._transport.reopen()
            welcome = self._transport.hello()
        except TransportClosed:
            self.stop()
            return
        except Exception:
            self._schedule_next_attempt()
            return
        new_id: Optional[str] = None
        if isinstance(welcome, dict):
            raw_id = welcome.get("daemon_instance_id")
            if isinstance(raw_id, str):
                new_id = raw_id
        if self._status is not None:
            try:
                self._status.bootstrap()
            except Exception:
                self._schedule_next_attempt()
                return
        for pin in list(self._subscribed_pins):
            try:
                self._transport.subscribe_pin(pin)
            except Exception:
                pass  # best effort; a pin that fails will be retried next loop
        # Restart the SUB pump if the transport supports background pumping
        # (it was stopped by reopen()).
        start_sub = getattr(self._transport, "start_background_sub_thread", None)
        if start_sub is not None:
            try:
                start_sub()
            except Exception:
                pass
        # Successful reconnect — reset counters and resume the ping loop.
        self._missed = 0
        self._backoff_s = self._backoff_initial_s
        self._attempt = 0
        self._set_state(STATE_CONNECTED)
        self._ping_timer.start()
        # Daemon-restart detection: only fire if we have a baseline id and
        # the new id differs. If we never had a baseline, store the new one
        # silently (this can happen when the very first hello races).
        if (
            self._daemon_instance_id is not None
            and new_id is not None
            and new_id != self._daemon_instance_id
        ):
            self._daemon_instance_id = new_id
            self.daemon_restarted.emit()
        elif new_id is not None:
            self._daemon_instance_id = new_id

    def _schedule_next_attempt(self) -> None:
        self._set_state(STATE_DISCONNECTED)
        self._backoff_s = min(self._backoff_s * 2.0, self._backoff_max_s)
        self._reconnect_timer.start(int(self._backoff_s * 1000))
