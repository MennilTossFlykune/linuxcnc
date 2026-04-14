"""qtcnc client bootstrap.

Two entry points:

* `bootstrap_programmatic(window, handler_cls, transport, config)` —
  take an already-loaded window and handler class, run the full
  bootstrap sequence: hello → snapshot → collect pins → declare pins →
  wire signals → on_startup → show → on_ready. Returns a
  `BootstrapResult` with references to every piece of the wiring so
  tests and callers can inspect state.

* `main(argv)` — CLI entry point. Parses `--ini`, `--screen`,
  `--mock`, and `--server-endpoint`; resolves the screen directory;
  loads `main.ui` via `qtpy.uic.loadUi`; imports `handler.py`; finds the
  `QtcncHandler` subclass; calls `bootstrap_programmatic`; then runs
  `app.exec()`.

The two-level split is deliberate: `bootstrap_programmatic` is fully
testable (no file IO, no argparse) while `main` handles the messy
filesystem and CLI concerns.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from qtpy.QtWidgets import QMainWindow

from qtcnc.core.command import Command
from qtcnc.core.config import QtcncConfig
from qtcnc.core.devices import Devices
from qtcnc.core.hal_spec import HalPinSpec
from qtcnc.core.status import Status
from qtcnc.handler import (
    HandlerContext,
    QtcncHandler,
    WidgetTree,
    auto_connect_handler,
)
from qtcnc.transport.base import Transport
from qtcnc.transport.reconnector import Reconnector
from qtcnc.widgets.base import QtcncWidget, collect_pin_declarations
from qtcnc.widgets.hal import HalPinHub


@dataclass
class BootstrapResult:
    """Everything the bootstrap produced. Returned so callers can hold references."""

    window: QMainWindow
    handler: QtcncHandler
    transport: Transport
    status: Status
    command: Command
    hal: HalPinHub
    config: QtcncConfig
    reconnector: Reconnector
    devices: Devices
    declared_pins: list[HalPinSpec] = field(default_factory=list)
    created_pins: list[str] = field(default_factory=list)
    widgets: list[QtcncWidget] = field(default_factory=list)


def bootstrap_programmatic(
    window: QMainWindow,
    handler_cls: type[QtcncHandler],
    transport: Transport,
    config: Optional[QtcncConfig] = None,
    *,
    show: bool = True,
) -> BootstrapResult:
    """Run the full bootstrap sequence on a pre-constructed window.

    Sequence, in order:

    1. Attach `qtcnc_status`, `qtcnc_command`, `qtcnc_hal` to the window.
    2. `transport.hello()` — triggers `_on_connected` callback.
    3. `status.bootstrap()` — pull snapshot, fan out initial signals.
    4. Walk widget tree, collect `HalPinSpec`s from every `QtcncWidget`.
    5. Construct the handler, merge `handler.declare_pins()` into the
       pin spec list.
    6. `transport.declare_pins()` — one call, locks HAL.
    7. Register a `HalPin` proxy on the hub for every created pin.
    8. Call `widget.qtcnc_setup()` on every QtcncWidget so they wire
       their own Status connections.
    9. `auto_connect_handler()` — route overridden on_* methods to
       matching Status signals.
    10. `handler.on_startup()`
    11. `window.show()` (unless `show=False`)
    12. `handler.on_ready()`

    Returns a `BootstrapResult` with references to everything.
    """
    if config is None:
        config = QtcncConfig.mock_default()

    status = Status(transport)
    command = Command(transport)
    hub = HalPinHub(transport)

    window.qtcnc_status = status
    window.qtcnc_command = command
    window.qtcnc_hal = hub
    window.qtcnc_transport = transport
    window.qtcnc_config = config

    welcome = transport.hello()
    status.bootstrap()

    # The Reconnector tracks PING liveness, recovers on daemon death, and
    # re-fires `daemon_restarted` if the daemon comes back with a fresh
    # `daemon_instance_id`. Bootstrap creates it but does NOT start the
    # ping loop — the CLI `main()` starts it after `app.exec()`; tests
    # that want to exercise it call `result.reconnector.start()` explicitly.
    reconnector = Reconnector(transport, status=status, parent=window)
    if isinstance(welcome, dict):
        raw_id = welcome.get("daemon_instance_id")
        if isinstance(raw_id, str):
            reconnector.record_initial_id(raw_id)
    status.attach_reconnector(reconnector)
    window.qtcnc_reconnector = reconnector

    # Client-local device / mount monitor. Never goes over the wire —
    # the operator's USB stick lives on the operator's workstation.
    # Bootstrap constructs it but leaves `start()` to the CLI entry
    # point (mirrors the reconnector pattern), so tests that bootstrap
    # programmatically don't accidentally open a pyudev netlink socket.
    devices = Devices(parent=window)
    window.qtcnc_devices = devices

    # Build handler context and instantiate the handler.
    ctx = HandlerContext(
        window=window,
        status=status,
        command=command,
        widgets=WidgetTree(window),
        hal=hub,
        config=config,
        devices=devices,
    )
    handler = handler_cls(ctx)
    window.qtcnc_handler = handler

    # Collect widgets and their declared pins.
    widgets = _find_qtcnc_widgets(window)
    declared = collect_pin_declarations(widgets)
    declared.extend(handler.declare_pins())

    # Dedupe on name, hard-fail on collision. (collect_pin_declarations
    # already caught collisions among widget-declared pins; we still need
    # to check handler pins against widget pins.)
    seen: dict[str, str] = {spec.name: "<widgets>" for spec in declared[: len(declared) - len(handler.declare_pins())]}
    for spec in handler.declare_pins():
        if spec.name in seen:
            raise ValueError(
                f"HAL pin {spec.name!r} declared by both {seen[spec.name]} and handler"
            )
        seen[spec.name] = "<handler>"

    # Push to transport.
    created_names: list[str] = []
    if declared:
        result = transport.declare_pins(declared)
        created_names = result.created
        if result.inherited:
            print(
                f"[qtcnc] inherited {len(created_names)} HAL pins from"
                f" existing daemon session",
                file=sys.stderr,
            )
        for name in created_names:
            hub.get_or_create(name)

    # Wire widgets.
    for w in widgets:
        if not getattr(w, "_qtcnc_setup_done", False):
            w.qtcnc_setup()
            w._qtcnc_setup_done = True

    # Wire handler's on_* hooks.
    auto_connect_handler(handler)

    # Lifecycle
    handler.on_startup()
    if show:
        window.show()
    handler.on_ready()

    return BootstrapResult(
        window=window,
        handler=handler,
        transport=transport,
        status=status,
        command=command,
        hal=hub,
        config=config,
        reconnector=reconnector,
        devices=devices,
        declared_pins=declared,
        created_pins=created_names,
        widgets=widgets,
    )


def _find_qtcnc_widgets(window: QMainWindow) -> list[QtcncWidget]:
    """Walk the window's child tree and return every QtcncWidget instance."""
    # QWidget.findChildren returns all descendants of a given class. Since
    # QtcncWidget is a Python mixin (not a Qt class), we can't pass it to
    # findChildren directly — Qt can't introspect it. Instead, enumerate all
    # QWidget descendants and filter by isinstance.
    from qtpy.QtWidgets import QWidget
    out: list[QtcncWidget] = []
    for child in window.findChildren(QWidget):
        if isinstance(child, QtcncWidget):
            out.append(child)
    return out


# ---------------------------------------------------------------------------
# CLI entry point — file-based bootstrap
# ---------------------------------------------------------------------------


def _resolve_screen_dir(name: str) -> Path:
    """Locate a screen directory by name.

    Search order: `$QTCNC_SCREEN_PATH`, then `~/linuxcnc/qtcnc/screens/<name>`,
    then `share/qtcnc/screens/<name>` relative to the package install root.
    """
    candidates: list[Path] = []
    env = os.environ.get("QTCNC_SCREEN_PATH")
    if env:
        for base in env.split(os.pathsep):
            candidates.append(Path(base) / name)
    candidates.append(Path.home() / "linuxcnc" / "qtcnc" / "screens" / name)
    pkg_root = Path(__file__).resolve().parents[3]
    candidates.append(pkg_root / "share" / "qtcnc" / "screens" / name)
    for c in candidates:
        if (c / "main.ui").exists() and (c / "handler.py").exists():
            return c
    raise FileNotFoundError(
        f"screen {name!r} not found; searched: {[str(c) for c in candidates]}"
    )


def _load_handler_class(handler_path: Path) -> type[QtcncHandler]:
    """Import handler.py and return the QtcncHandler subclass defined there."""
    spec = importlib.util.spec_from_file_location("qtcnc_user_handler", handler_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import handler from {handler_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, QtcncHandler) and obj is not QtcncHandler:
            return obj
    raise ImportError(f"no QtcncHandler subclass found in {handler_path}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="qtcnc", description="qtcnc GUI client")
    p.add_argument("--ini", help="LinuxCNC INI file to read cosmetic settings from")
    p.add_argument("--screen", default="minimal", help="screen name (default: minimal)")
    p.add_argument("--mock", action="store_true", help="use MockTransport; no daemon")
    p.add_argument("--server-endpoint", help="ZMQ endpoint of qtcnc-serverd")
    p.add_argument("--curve-server-key",
                   help="path to the daemon's public *.key file (enables encryption)")
    p.add_argument("--curve-client-key",
                   help="path to this client's *.key_secret file")
    p.add_argument("--list-screens", action="store_true", help="print available screens and exit")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    from qtpy.QtWidgets import QApplication
    from qtpy import uic

    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.list_screens:
        # Minimal implementation: enumerate known search paths.
        print("available screens:")
        for base in (Path.home() / "linuxcnc" / "qtcnc" / "screens",
                     Path(__file__).resolve().parents[3] / "share" / "qtcnc" / "screens"):
            if base.exists():
                for d in sorted(base.iterdir()):
                    if d.is_dir():
                        print(f"  {d.name}  ({d})")
        return 0

    app = QApplication.instance() or QApplication(sys.argv)

    screen_dir = _resolve_screen_dir(args.screen)
    handler_cls = _load_handler_class(screen_dir / "handler.py")

    window = uic.loadUi(str(screen_dir / "main.ui"))
    if not isinstance(window, QMainWindow):
        # If the UI root isn't a QMainWindow, wrap it.
        mw = QMainWindow()
        mw.setCentralWidget(window)
        window = mw

    if args.mock:
        from qtcnc.transport.mock import MockTransport
        transport: Transport = MockTransport()
        config = (
            QtcncConfig.from_ini(args.ini) if args.ini else QtcncConfig.mock_default()
        )
    elif args.server_endpoint:
        from qtcnc.transport.qt_marshaller import QtMarshaller, QtMarshalledTransport
        from qtcnc.transport.zmq_client import ZmqClientTransport

        curve_kwargs: dict[str, Any] = {}
        if args.curve_server_key or args.curve_client_key:
            if not (args.curve_server_key and args.curve_client_key):
                raise SystemExit(
                    "--curve-server-key and --curve-client-key must be set together",
                )
            from zmq.auth.certs import load_certificate
            server_pub, _ = load_certificate(args.curve_server_key)
            client_pub, client_sec = load_certificate(args.curve_client_key)
            if server_pub is None:
                raise SystemExit(
                    f"{args.curve_server_key} has no public key",
                )
            if client_pub is None or client_sec is None:
                raise SystemExit(
                    f"{args.curve_client_key} must contain both public and secret keys",
                )
            curve_kwargs = {
                "curve_server_key": server_pub,
                "curve_public_key": client_pub,
                "curve_secret_key": client_sec,
            }

        inner = ZmqClientTransport(args.server_endpoint, **curve_kwargs)
        marshaller = QtMarshaller(parent=window)
        transport = QtMarshalledTransport(inner, marshaller)
        config = (
            QtcncConfig.from_ini(args.ini) if args.ini else QtcncConfig.mock_default()
        )
    else:
        raise SystemExit("must specify --mock or --server-endpoint")

    result = bootstrap_programmatic(window, handler_cls, transport, config=config)

    # Spawn the background SUB thread (no-op for MockTransport).
    start_sub = getattr(transport, "start_background_sub_thread", None)
    if start_sub is not None:
        start_sub()
    # Start the PING loop unless explicitly running in mock mode (the mock
    # transport has no daemon to lose, so the loop would just spin idly).
    if not args.mock:
        result.reconnector.start()
    # Devices monitor always starts — operator's USB stick is local even
    # in mock mode, and the monitor degrades gracefully without pyudev.
    result.devices.start()

    try:
        return app.exec()
    finally:
        try:
            result.devices.stop()
        except Exception:
            pass
        try:
            result.reconnector.stop()
        except Exception:
            pass
        stop_sub = getattr(transport, "stop_background_sub_thread", None)
        if stop_sub is not None:
            try:
                stop_sub()
            except Exception:
                pass
        try:
            transport.close()
        except Exception:
            pass
