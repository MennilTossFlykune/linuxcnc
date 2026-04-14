"""Client-side device + mount monitor.

`Devices` is a QObject that emits signals when a USB stick (or any
removable block device) is inserted or removed, and when a filesystem
mount shows up under `/proc/self/mountinfo`. The two axes are reported
independently because the gap between "kernel sees the device" and
"user has a mount they can write to" is several seconds for automounted
media — handlers that just want to copy a file from a USB stick care
about `mount_added`, not `device_added`.

Transport: this module never goes over ZMQ. The operator's USB stick
lives on the operator's workstation; shipping udev events to a remote
daemon would be wrong. `Devices` is instantiated directly on the
client side inside `bootstrap_programmatic`, and its signals flow into
`QtcncHandler.on_device_*` / `on_mount_*` hooks through the usual
auto-connect path.

Graceful degradation: if `pyudev` isn't installed (or the host isn't
running udev at all), `start()` silently skips the netlink monitor and
only runs the mountinfo poll. Handlers still see `mount_added`/
`mount_removed` — just without the device-level metadata.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Optional

from qtpy.QtCore import QObject, QThread, QTimer, Signal, Slot

from qtcnc.core.types import DeviceInfo, MountInfo


try:
    import pyudev  # type: ignore[import-not-found]
except ImportError:
    pyudev = None  # type: ignore[assignment]


_POLL_INTERVAL_MS = 1000  # mount poll cadence; cheap at 1 Hz


class Devices(QObject):
    """Emits signals for USB devices and filesystem mounts.

    Lifecycle:

    * `start()` — spin up the udev monitor thread (if pyudev is
      installed) and the mount-poll `QTimer`. Seeds the initial state.
    * `stop()` — tear everything down. Idempotent; safe to call twice.

    Signals:

    * `device_added(DeviceInfo)` — a new partition or removable block
      device appeared on the udev netlink. Only emitted when pyudev is
      available.
    * `device_removed(DeviceInfo)` — the inverse.
    * `mount_added(MountInfo)` — a new entry appeared in
      `/proc/self/mountinfo` since the last poll.
    * `mount_removed(MountInfo)` — an entry vanished.
    """

    device_added = Signal(object)
    device_removed = Signal(object)
    mount_added = Signal(object)
    mount_removed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._udev_thread: Optional[_UdevThread] = None
        self._mount_timer: Optional[QTimer] = None
        self._mounts: dict[str, MountInfo] = {}  # keyed by target
        self._mountinfo_path: str = "/proc/self/mountinfo"
        self._started: bool = False

    # ----- public lifecycle -----

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Seed mounts first so start-up doesn't fire `mount_added` for
        # every pre-existing bind mount. Then start the timer and thread.
        self._mounts = self._read_mounts()
        self._mount_timer = QTimer(self)
        self._mount_timer.setInterval(_POLL_INTERVAL_MS)
        self._mount_timer.timeout.connect(self._poll_mounts)
        self._mount_timer.start()
        if pyudev is not None:
            try:
                self._udev_thread = _UdevThread(self._on_udev_event, parent=self)
                self._udev_thread.start()
            except Exception:
                # pyudev present but the netlink socket wouldn't open
                # (e.g. inside an unprivileged container). Fall back to
                # mount-only mode instead of crashing the client.
                self._udev_thread = None

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._mount_timer is not None:
            self._mount_timer.stop()
            self._mount_timer.deleteLater()
            self._mount_timer = None
        if self._udev_thread is not None:
            self._udev_thread.requestInterruption()
            self._udev_thread.wait(2000)
            self._udev_thread = None

    # ----- test / introspection helpers -----

    @property
    def mounts(self) -> dict[str, MountInfo]:
        """Current mount set, keyed by target path. Copy — safe to mutate."""
        return dict(self._mounts)

    def set_mountinfo_path(self, path: str) -> None:
        """Point the poller at a non-default mountinfo file.

        Tests monkey-patch this to feed a synthetic mountinfo through
        the diff pipeline without a real /proc.
        """
        self._mountinfo_path = path
        if self._started:
            self._mounts = self._read_mounts()

    def poll_now(self) -> None:
        """Force a mount-info diff check right now.

        Exposed so tests can drive the poll manually instead of waiting
        for the `_POLL_INTERVAL_MS` timer to fire.
        """
        self._poll_mounts()

    # ----- internals -----

    def _poll_mounts(self) -> None:
        try:
            new = self._read_mounts()
        except OSError:
            return
        old = self._mounts
        for target, info in new.items():
            if target not in old:
                self.mount_added.emit(info)
        for target, info in old.items():
            if target not in new:
                self.mount_removed.emit(info)
        self._mounts = new

    def _read_mounts(self) -> dict[str, MountInfo]:
        out: dict[str, MountInfo] = {}
        try:
            with open(self._mountinfo_path, "r", encoding="utf-8") as f:
                for raw in f:
                    info = _parse_mountinfo_line(raw)
                    if info is not None:
                        out[info.target] = info
        except OSError:
            return {}
        return out

    @Slot(object, object)
    def _on_udev_event(self, action: str, info: DeviceInfo) -> None:
        """Called from the udev thread via a queued signal.

        `_UdevThread` posts `(action, DeviceInfo)` tuples through
        `QMetaObject.invokeMethod` so every emit happens on the main
        thread — keeps the Qt threading contract intact.
        """
        if action == "add":
            self.device_added.emit(info)
        elif action == "remove":
            self.device_removed.emit(info)


def _parse_mountinfo_line(raw: str) -> Optional[MountInfo]:
    """Parse a single `/proc/self/mountinfo` line into a `MountInfo`.

    Format per `man 5 proc`:

        36 35 98:0 /mnt1 /mnt/oldroot rw,noatime master:1 - ext3 /dev/sda1 rw

    The `-` separates the "mount options" fields (pre) from "fs type
    source super-options" (post). Splitting on ` - ` keeps the parser
    small and robust against extra optional fields between mount_point
    and the separator.
    """
    try:
        pre, sep, post = raw.partition(" - ")
        if not sep:
            return None
        pre_fields = pre.split()
        post_fields = post.split()
        if len(pre_fields) < 5 or len(post_fields) < 2:
            return None
        target = pre_fields[4]
        fstype = post_fields[0]
        source = post_fields[1]
        return MountInfo(source=source, target=target, fstype=fstype)
    except Exception:
        return None


class _UdevThread(QThread):
    """Background thread that blocks on `pyudev.Monitor.poll()`.

    Uses a short poll timeout (250 ms) so `requestInterruption()` /
    `wait()` shuts the thread down promptly. The thread never touches
    Qt state directly: each received event is forwarded to `_cb` via
    `QMetaObject.invokeMethod(..., Qt.QueuedConnection)` so `_cb` runs
    on the main thread.
    """

    def __init__(
        self, cb: Callable[[str, DeviceInfo], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._cb = cb
        # The monitor itself is opened inside `run()` so a pyudev
        # import error surfaces on the thread that can handle it.
        self._monitor: Any = None

    def run(self) -> None:  # pragma: no cover — exercised on real hw only
        if pyudev is None:
            return
        ctx = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(ctx)
        monitor.filter_by("block")
        self._monitor = monitor
        while not self.isInterruptionRequested():
            # `poll()` returns None on timeout, a Device on event.
            try:
                device = monitor.poll(timeout=0.25)
            except Exception:
                break
            if device is None:
                continue
            if device.device_type != "partition":
                continue
            action = device.action or ""
            info = DeviceInfo(
                node=device.device_node or "",
                subsystem=device.subsystem or "",
                dev_type=device.device_type or "",
                vendor=device.properties.get("ID_VENDOR", "") or "",
                model=device.properties.get("ID_MODEL", "") or "",
                serial=device.properties.get("ID_SERIAL_SHORT", "") or "",
            )
            # Invoke on the parent QObject's thread so signal fan-out
            # stays on main.
            from qtpy.QtCore import QMetaObject, Qt, Q_ARG
            target = self.parent()
            if target is None:
                continue
            QMetaObject.invokeMethod(
                target, "_on_udev_event",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(object, action),
                Q_ARG(object, info),
            )
