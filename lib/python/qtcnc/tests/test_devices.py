"""Tests for `qtcnc.core.devices`.

The udev half is gated on `pyudev` being importable (and even then is
hard to exercise deterministically without a real USB insertion), so
these tests concentrate on the `/proc/self/mountinfo` poll path.
Technique: point `Devices.set_mountinfo_path(...)` at a file we control
under `tmp_path`, call `poll_now()` to force a diff check, and assert
the `mount_added`/`mount_removed` signals fire with the right
`MountInfo` payloads.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qtpy.QtCore import QObject
from qtpy.QtWidgets import QApplication

from qtcnc.core.devices import Devices, _parse_mountinfo_line
from qtcnc.core.types import MountInfo


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


# Sample lines pulled from a real `/proc/self/mountinfo` (trimmed).
_SAMPLE_MOUNTINFO = """\
22 28 0:20 / /sys rw,nosuid,nodev,noexec,relatime shared:7 - sysfs sysfs rw
23 28 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw
28 1 253:0 / / rw,relatime shared:1 - ext4 /dev/mapper/root rw,errors=remount-ro
"""


class TestMountinfoLineParsing:
    def test_parses_standard_line(self) -> None:
        line = "28 1 253:0 / / rw,relatime shared:1 - ext4 /dev/mapper/root rw\n"
        info = _parse_mountinfo_line(line)
        assert info is not None
        assert info.target == "/"
        assert info.source == "/dev/mapper/root"
        assert info.fstype == "ext4"

    def test_parses_bind_mount(self) -> None:
        line = "120 28 0:53 / /media/usb rw,noatime - vfat /dev/sdb1 rw,utf8\n"
        info = _parse_mountinfo_line(line)
        assert info is not None
        assert info.target == "/media/usb"
        assert info.source == "/dev/sdb1"
        assert info.fstype == "vfat"

    def test_rejects_line_without_separator(self) -> None:
        line = "28 1 253:0 / / rw ext4 /dev/root\n"
        assert _parse_mountinfo_line(line) is None

    def test_rejects_empty_line(self) -> None:
        assert _parse_mountinfo_line("\n") is None
        assert _parse_mountinfo_line("") is None


# ---------------------------------------------------------------------------
# Mount diff smoke tests via a synthetic mountinfo file
# ---------------------------------------------------------------------------


def _write_mountinfo(path: Path, body: str) -> None:
    path.write_text(body)


class TestDevicesMountDiff:
    def test_start_seeds_without_firing(self, tmp_path: Path) -> None:
        mi = tmp_path / "mountinfo"
        _write_mountinfo(mi, _SAMPLE_MOUNTINFO)
        dev = Devices()
        dev.set_mountinfo_path(str(mi))
        added: list[MountInfo] = []
        removed: list[MountInfo] = []
        dev.mount_added.connect(lambda m: added.append(m))
        dev.mount_removed.connect(lambda m: removed.append(m))
        dev.start()
        try:
            # start() seeds from the current file, so no events should
            # fire until a subsequent poll picks up a change.
            assert added == []
            assert removed == []
            # The seed is nonetheless visible via .mounts.
            assert "/" in dev.mounts
            assert dev.mounts["/"].fstype == "ext4"
        finally:
            dev.stop()

    def test_added_mount_fires_signal(self, tmp_path: Path) -> None:
        mi = tmp_path / "mountinfo"
        _write_mountinfo(mi, _SAMPLE_MOUNTINFO)
        dev = Devices()
        dev.set_mountinfo_path(str(mi))
        added: list[MountInfo] = []
        dev.mount_added.connect(lambda m: added.append(m))
        dev.start()
        try:
            extra = "120 28 0:53 / /media/usb rw,noatime - vfat /dev/sdb1 rw\n"
            _write_mountinfo(mi, _SAMPLE_MOUNTINFO + extra)
            dev.poll_now()
            assert len(added) == 1
            assert added[0].target == "/media/usb"
            assert added[0].source == "/dev/sdb1"
            assert added[0].fstype == "vfat"
        finally:
            dev.stop()

    def test_removed_mount_fires_signal(self, tmp_path: Path) -> None:
        mi = tmp_path / "mountinfo"
        body = _SAMPLE_MOUNTINFO + "120 28 0:53 / /media/usb rw - vfat /dev/sdb1 rw\n"
        _write_mountinfo(mi, body)
        dev = Devices()
        dev.set_mountinfo_path(str(mi))
        removed: list[MountInfo] = []
        dev.mount_removed.connect(lambda m: removed.append(m))
        dev.start()
        try:
            _write_mountinfo(mi, _SAMPLE_MOUNTINFO)  # usb stick went away
            dev.poll_now()
            assert len(removed) == 1
            assert removed[0].target == "/media/usb"
        finally:
            dev.stop()

    def test_no_spurious_events_on_identical_poll(self, tmp_path: Path) -> None:
        mi = tmp_path / "mountinfo"
        _write_mountinfo(mi, _SAMPLE_MOUNTINFO)
        dev = Devices()
        dev.set_mountinfo_path(str(mi))
        events: list[str] = []
        dev.mount_added.connect(lambda _m: events.append("add"))
        dev.mount_removed.connect(lambda _m: events.append("rem"))
        dev.start()
        try:
            for _ in range(5):
                dev.poll_now()
            assert events == []
        finally:
            dev.stop()

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        dev = Devices()
        dev.set_mountinfo_path(str(tmp_path / "missing"))
        dev.start()
        dev.stop()
        dev.stop()  # second call must not raise

    def test_missing_mountinfo_file_is_quiet(self, tmp_path: Path) -> None:
        dev = Devices()
        dev.set_mountinfo_path(str(tmp_path / "does-not-exist"))
        dev.start()
        try:
            # Should not raise; mounts is empty because the file isn't there.
            assert dev.mounts == {}
            dev.poll_now()
            assert dev.mounts == {}
        finally:
            dev.stop()


# ---------------------------------------------------------------------------
# pyudev path is gated — present here so the test file documents the gate,
# but it's a bare smoke check that just verifies start/stop don't blow up.
# ---------------------------------------------------------------------------


class TestDevicesPyudevGate:
    def test_start_stop_without_pyudev_ok(self, tmp_path: Path) -> None:
        """If pyudev isn't importable, start() must still succeed in
        mount-only mode and stop() must work cleanly."""
        mi = tmp_path / "mi"
        _write_mountinfo(mi, _SAMPLE_MOUNTINFO)
        dev = Devices()
        dev.set_mountinfo_path(str(mi))
        dev.start()
        try:
            # At minimum, mount polling is active.
            assert dev._mount_timer is not None
            assert dev._mount_timer.isActive()
        finally:
            dev.stop()
