"""End-to-end reconnect test for A3.

Spins a real `QtcncServer` over ipc, connects a real `ZmqClientTransport`,
attaches a `Reconnector`, then forcibly stops the server, restarts it on
the same endpoint, and asserts the client recovers — including the
`daemon_restarted` signal firing because the new server has a fresh
`daemon_instance_id`.

This catches issues that the in-process MockTransport tests can't:

* The REQ socket really does get poisoned on timeout and `reopen()` must
  rebuild it.
* The SUB background thread holds a stale socket reference after the
  server vanishes; `reopen()` must stop it before the new sockets are
  created.
* The new server publishes its own `daemon_instance_id` over the wire
  in `WELCOME` and the Reconnector reads it off the dict correctly.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

zmq = pytest.importorskip("zmq")

from qtcnc.server import QtcncServer
from qtcnc.transport.base import TransportError
from qtcnc.transport.reconnector import (
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    Reconnector,
)
from qtcnc.transport.zmq_client import ZmqClientTransport

from qtcnc.tests.test_server import (
    _FakeHalModule,
    _FakeLinuxcncModule,
    _ServerThread,
)


@pytest.fixture
def endpoint() -> str:
    base = f"ipc:///tmp/qtcnc-recon-{uuid.uuid4().hex[:12]}"
    yield base
    for suffix in (".pub", ".cmd"):
        path = base[len("ipc://"):] + suffix
        try:
            os.remove(path)
        except OSError:
            pass


def _make_server(endpoint: str) -> tuple[QtcncServer, _ServerThread]:
    lc = _FakeLinuxcncModule()
    halmod = _FakeHalModule()
    server = QtcncServer(
        endpoint,
        linuxcnc_module=lc,
        hal_module=halmod,
        poll_interval_s=0.02,
    )
    thread = _ServerThread(server)
    thread.start()
    return server, thread


def _stop_server(server: QtcncServer, thread: _ServerThread) -> None:
    thread.shutdown()
    try:
        server.stop()
    except Exception:
        pass


class TestZmqReconnect:
    def test_client_survives_server_restart(self, endpoint: str) -> None:
        server, thread = _make_server(endpoint)
        try:
            client = ZmqClientTransport(endpoint, request_timeout_ms=300)
            try:
                welcome = client.hello()
                first_id = welcome.get("daemon_instance_id")
                assert isinstance(first_id, str) and first_id
                snap = client.get_snapshot()
                assert snap is not None

                r = Reconnector(
                    client,
                    ping_hz=20.0,
                    miss_threshold=2,
                    backoff_initial_s=0.05,
                    backoff_max_s=0.1,
                )
                r.record_initial_id(first_id)
                ids: list = []
                states: list[str] = []
                r.daemon_restarted.connect(lambda: ids.append("restart"))
                r.state_changed.connect(states.append)

                # Kill the server.
                _stop_server(server, thread)
                # Give zmq a beat to drop the connection.
                time.sleep(0.05)

                # Drive ping until it trips. Each call eats one nonce.
                for _ in range(5):
                    r._on_ping_timer()
                    if r.state == STATE_DISCONNECTED:
                        break
                assert r.state == STATE_DISCONNECTED

                # Restart on the same endpoint.
                server2, thread2 = _make_server(endpoint)
                try:
                    # Wait for the new bind to settle.
                    time.sleep(0.1)
                    r._try_reconnect()
                    assert r.state == STATE_CONNECTED
                    assert ids == ["restart"]
                    # New id must be tracked.
                    assert r.daemon_instance_id != first_id
                    # Subsequent pings work against the new server.
                    r._on_ping_timer()
                    assert r.missed == 0
                finally:
                    _stop_server(server2, thread2)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        finally:
            # Best-effort cleanup if the test failed before stopping.
            try:
                _stop_server(server, thread)
            except Exception:
                pass

    def test_req_poison_clears_on_reopen(self, endpoint: str) -> None:
        """A timed-out REQ stays poisoned until reopen() is called."""
        server, thread = _make_server(endpoint)
        try:
            client = ZmqClientTransport(endpoint, request_timeout_ms=200)
            try:
                client.hello()
                _stop_server(server, thread)
                # First request after server death should time out and
                # poison the REQ socket.
                with pytest.raises(TransportError):
                    client.ping()
                assert client._req_poisoned is True
                # Subsequent calls also fail with a poison-aware message.
                with pytest.raises(TransportError):
                    client.get_snapshot()

                # Bring the server back and reopen.
                server2, thread2 = _make_server(endpoint)
                try:
                    time.sleep(0.1)
                    client.reopen()
                    assert client._req_poisoned is False
                    welcome = client.hello()
                    assert "daemon_instance_id" in welcome
                finally:
                    _stop_server(server2, thread2)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        finally:
            try:
                _stop_server(server, thread)
            except Exception:
                pass
