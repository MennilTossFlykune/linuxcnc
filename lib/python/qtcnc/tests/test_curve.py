"""End-to-end CURVE auth tests for A4.

Generates ephemeral CURVE keypairs in a tmp dir, spins a real
`QtcncServer` configured with curve_server + an authorized-clients
directory, then drives a `ZmqClientTransport` against it. Verifies:

* Authorized client: handshake succeeds, `WELCOME` reports
  `curve_enabled=True`, REQs work end-to-end.
* Unauthorized client (client whose public key is NOT in the daemon's
  authorized-clients dir): handshake silently fails — REQ never gets a
  reply, so `hello()` raises `TransportError` on the configured timeout.
* Validation: half-configured CURVE on either end raises `ValueError`
  before any socket is touched (so failures are loud, not silent).
* `CURVE_ALLOW_ANY` mode: when the daemon's `authorized_clients_dir` is
  None, any well-formed client key is accepted.

The reference `_FakeLinuxcncModule` and `_ServerThread` come from
`test_server.py` to keep the daemon spin-up identical across tests.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Optional

import pytest

zmq = pytest.importorskip("zmq")
zmq_auth = pytest.importorskip("zmq.auth")
from zmq.auth.certs import create_certificates, load_certificate

from qtcnc.server import QtcncServer
from qtcnc.transport.base import TransportError
from qtcnc.transport.zmq_client import ZmqClientTransport
from qtcnc.transport.zmq_server import ZmqServerTransport

from qtcnc.tests.test_server import (
    _FakeHalModule,
    _FakeLinuxcncModule,
    _ServerThread,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def endpoint() -> str:
    base = f"ipc:///tmp/qtcnc-curve-{uuid.uuid4().hex[:12]}"
    yield base
    for suffix in (".pub", ".cmd"):
        path = base[len("ipc://"):] + suffix
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def keypair_dir(tmp_path: Path) -> Path:
    """A tmp dir holding a `server` keypair, an authorized `client-good`
    keypair (with its public key copied into `authorized/`), and an
    unauthorized `client-bad` keypair (whose public key is NOT in the
    authorized dir)."""
    create_certificates(str(tmp_path), "server")
    create_certificates(str(tmp_path), "client-good")
    create_certificates(str(tmp_path), "client-bad")
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    # Drop only the "good" client's public key into the daemon's
    # authorized directory. The "bad" client has a valid keypair but the
    # daemon doesn't know about it — that's the point of the negative test.
    good_pub = tmp_path / "client-good.key"
    (authorized / "client-good.key").write_bytes(good_pub.read_bytes())
    return tmp_path


def _load_keys(directory: Path, name: str) -> tuple[bytes, bytes]:
    """Return (public, secret) raw bytes for a given key basename."""
    public, _ = load_certificate(str(directory / f"{name}.key"))
    pub_again, secret = load_certificate(str(directory / f"{name}.key_secret"))
    assert public is not None and secret is not None and pub_again is not None
    return public, secret


def _make_server(
    endpoint: str,
    *,
    server_pub: bytes,
    server_sec: bytes,
    authorized_dir: Optional[str],
) -> tuple[QtcncServer, _ServerThread]:
    lc = _FakeLinuxcncModule()
    halmod = _FakeHalModule()
    server = QtcncServer(
        endpoint,
        linuxcnc_module=lc,
        hal_module=halmod,
        poll_interval_s=0.02,
        curve_public_key=server_pub,
        curve_secret_key=server_sec,
        authorized_clients_dir=authorized_dir,
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


# ---------------------------------------------------------------------------
# Validation: half-configured CURVE rejects at construction
# ---------------------------------------------------------------------------


class TestCurveValidation:
    def test_server_half_configured_rejects(self) -> None:
        """Providing only one of (pub, secret) to the server raises."""
        with pytest.raises(ValueError, match="must be set together"):
            ZmqServerTransport(
                "ipc:///tmp/qtcnc-curve-validate-pub",
                handler=None,  # type: ignore[arg-type]
                curve_public_key=b"x" * 32,
                curve_secret_key=None,
            )
        with pytest.raises(ValueError, match="must be set together"):
            ZmqServerTransport(
                "ipc:///tmp/qtcnc-curve-validate-sec",
                handler=None,  # type: ignore[arg-type]
                curve_public_key=None,
                curve_secret_key=b"x" * 32,
            )

    def test_client_half_configured_rejects(self) -> None:
        """All three client CURVE keys must travel together."""
        with pytest.raises(ValueError, match="must all be set together"):
            ZmqClientTransport(
                "ipc:///tmp/qtcnc-curve-validate-client",
                curve_server_key=b"x" * 32,
                curve_public_key=None,
                curve_secret_key=b"y" * 32,
            )
        with pytest.raises(ValueError, match="must all be set together"):
            ZmqClientTransport(
                "ipc:///tmp/qtcnc-curve-validate-client2",
                curve_server_key=None,
                curve_public_key=b"x" * 32,
                curve_secret_key=b"y" * 32,
            )


# ---------------------------------------------------------------------------
# Positive paths: handshake succeeds, REQs work
# ---------------------------------------------------------------------------


class TestCurveHandshake:
    def test_authorized_client_hello_roundtrip(
        self, endpoint: str, keypair_dir: Path,
    ) -> None:
        """An authorized client gets a clean WELCOME with curve_enabled=True."""
        server_pub, server_sec = _load_keys(keypair_dir, "server")
        client_pub, client_sec = _load_keys(keypair_dir, "client-good")
        server, thread = _make_server(
            endpoint,
            server_pub=server_pub,
            server_sec=server_sec,
            authorized_dir=str(keypair_dir / "authorized"),
        )
        try:
            client = ZmqClientTransport(
                endpoint,
                request_timeout_ms=2000,
                curve_server_key=server_pub,
                curve_public_key=client_pub,
                curve_secret_key=client_sec,
            )
            try:
                welcome = client.hello()
                assert welcome.get("curve_enabled") is True
                assert isinstance(welcome.get("daemon_instance_id"), str)
                # And a follow-up REQ also works (proves the encrypted
                # channel is durable, not just the initial handshake).
                snap = client.get_snapshot()
                assert snap is not None
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        finally:
            _stop_server(server, thread)

    def test_curve_allow_any_accepts_unknown_key(
        self, endpoint: str, keypair_dir: Path,
    ) -> None:
        """With authorized_clients_dir=None, any valid CURVE client gets in."""
        server_pub, server_sec = _load_keys(keypair_dir, "server")
        # Use the "bad" client — it's not in any allowlist. With
        # CURVE_ALLOW_ANY (the daemon mode when authorized_dir is None)
        # the daemon should still accept it.
        client_pub, client_sec = _load_keys(keypair_dir, "client-bad")
        server, thread = _make_server(
            endpoint,
            server_pub=server_pub,
            server_sec=server_sec,
            authorized_dir=None,
        )
        try:
            client = ZmqClientTransport(
                endpoint,
                request_timeout_ms=2000,
                curve_server_key=server_pub,
                curve_public_key=client_pub,
                curve_secret_key=client_sec,
            )
            try:
                welcome = client.hello()
                assert welcome.get("curve_enabled") is True
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        finally:
            _stop_server(server, thread)


# ---------------------------------------------------------------------------
# Negative path: unauthorized client never completes handshake
# ---------------------------------------------------------------------------


class TestCurveRejection:
    def test_unauthorized_client_handshake_fails(
        self, endpoint: str, keypair_dir: Path,
    ) -> None:
        """A client whose public key isn't in the authorized dir cannot HELLO.

        ZMQ's ZAP layer silently drops connections whose CURVE handshake
        fails authentication, so the client never sees a reply — `hello()`
        raises `TransportError` on the configured timeout instead of a
        clean refusal. That's the documented behavior; we just verify
        the failure is loud and bounded by the request timeout.
        """
        server_pub, server_sec = _load_keys(keypair_dir, "server")
        bad_pub, bad_sec = _load_keys(keypair_dir, "client-bad")
        server, thread = _make_server(
            endpoint,
            server_pub=server_pub,
            server_sec=server_sec,
            authorized_dir=str(keypair_dir / "authorized"),
        )
        try:
            # Give the daemon's ThreadAuthenticator a moment to settle.
            time.sleep(0.1)
            client = ZmqClientTransport(
                endpoint,
                request_timeout_ms=300,
                curve_server_key=server_pub,
                curve_public_key=bad_pub,
                curve_secret_key=bad_sec,
            )
            try:
                with pytest.raises(TransportError):
                    client.hello()
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        finally:
            _stop_server(server, thread)

    def test_wrong_server_key_handshake_fails(
        self, endpoint: str, keypair_dir: Path,
    ) -> None:
        """If the client trusts the wrong server public key, the CURVE
        handshake never completes and `hello()` times out."""
        server_pub, server_sec = _load_keys(keypair_dir, "server")
        good_pub, good_sec = _load_keys(keypair_dir, "client-good")
        # Intentionally swap in the "bad" client's PUBLIC key as the
        # purported server key. That's not the real server's public key,
        # so the client's CURVE state machine refuses to encrypt to a
        # peer that can't decrypt.
        wrong_server_pub, _ = _load_keys(keypair_dir, "client-bad")
        server, thread = _make_server(
            endpoint,
            server_pub=server_pub,
            server_sec=server_sec,
            authorized_dir=str(keypair_dir / "authorized"),
        )
        try:
            time.sleep(0.1)
            client = ZmqClientTransport(
                endpoint,
                request_timeout_ms=300,
                curve_server_key=wrong_server_pub,
                curve_public_key=good_pub,
                curve_secret_key=good_sec,
            )
            try:
                with pytest.raises(TransportError):
                    client.hello()
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        finally:
            _stop_server(server, thread)
