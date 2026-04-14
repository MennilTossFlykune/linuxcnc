"""Tests for qtcnc.bootstrap and qtcnc.core.config."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.bootstrap import (
    bootstrap_programmatic,
    _load_handler_class,
    _resolve_screen_dir,
)
from qtcnc.core.config import QtcncConfig
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.types import Position, TaskMode
from qtcnc.handler import HandlerContext, QtcncHandler
from qtcnc.signals import CommandVerb
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.common.dro import DroWidget
from qtcnc.widgets.common.estop_button import EstopButton
from qtcnc.widgets.common.machine_power_button import MachinePowerButton
from qtcnc.widgets.common.state_label import StateLabel


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestQtcncConfig:
    def test_mock_default(self):
        cfg = QtcncConfig.mock_default()
        assert cfg.window_title == "qtcnc (mock)"
        assert cfg.axis_count == 3
        assert cfg.ini_path is None

    def test_from_ini_parses_coordinates(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text(textwrap.dedent("""
            [DISPLAY]
            TITLE = Test Machine
            [TRAJ]
            COORDINATES = X Y Z A
        """).strip())
        cfg = QtcncConfig.from_ini(str(ini))
        assert cfg.window_title == "Test Machine"
        assert cfg.axis_count == 4
        assert cfg.axis_letters == "XYZA"

    def test_from_ini_missing_sections_uses_defaults(self, tmp_path):
        ini = tmp_path / "minimal.ini"
        ini.write_text("[EMC]\nVERSION = 1.1\n")
        cfg = QtcncConfig.from_ini(str(ini))
        assert cfg.window_title == "qtcnc"
        assert cfg.axis_count == 3


# ---------------------------------------------------------------------------
# A minimal programmatic handler and window
# ---------------------------------------------------------------------------


class _Handler(QtcncHandler):
    def __init__(self, ctx: HandlerContext) -> None:
        super().__init__(ctx)
        self.seen: list[str] = []
        self.extra_pins: list[HalPinSpec] = []

    def declare_pins(self) -> list[HalPinSpec]:
        return list(self.extra_pins)

    def on_startup(self) -> None:
        self.seen.append("on_startup")

    def on_ready(self) -> None:
        self.seen.append("on_ready")

    def on_estop(self, asserted: bool) -> None:
        self.seen.append(f"on_estop:{asserted}")


def _build_window():
    window = QMainWindow()
    estop = EstopButton(window)
    estop.setObjectName("estop_btn")
    power = MachinePowerButton(window)
    power.setObjectName("power_btn")
    dro = DroWidget(window)
    dro.setObjectName("dro_x")
    dro._set_axis(0)
    state = StateLabel(window)
    state.setObjectName("state_lbl")
    return window, (estop, power, dro, state)


# ---------------------------------------------------------------------------
# bootstrap_programmatic
# ---------------------------------------------------------------------------


class TestBootstrapProgrammatic:
    def test_runs_lifecycle_in_order(self):
        window, _ = _build_window()
        t = MockTransport()
        result = bootstrap_programmatic(window, _Handler, t, show=False)
        assert result.handler.seen[:2] == ["on_startup", "on_ready"]

    def test_attaches_qtcnc_objects_to_window(self):
        window, _ = _build_window()
        t = MockTransport()
        result = bootstrap_programmatic(window, _Handler, t, show=False)
        assert window.qtcnc_status is result.status
        assert window.qtcnc_command is result.command
        assert window.qtcnc_hal is result.hal
        assert window.qtcnc_transport is t
        assert window.qtcnc_handler is result.handler

    def test_collects_widgets(self):
        window, widgets = _build_window()
        t = MockTransport()
        result = bootstrap_programmatic(window, _Handler, t, show=False)
        # All four qtcnc widgets should be discovered.
        assert len(result.widgets) == 4
        for w in widgets:
            assert w in result.widgets

    def test_declares_dro_pin(self):
        window, _ = _build_window()
        t = MockTransport()
        result = bootstrap_programmatic(window, _Handler, t, show=False)
        pin_names = [s.name for s in result.declared_pins]
        assert "qtcnc.dro_x.value-out" in pin_names
        assert "qtcnc.dro_x.value-out" in result.created_pins

    def test_dro_pin_registered_on_hub(self):
        window, _ = _build_window()
        t = MockTransport()
        result = bootstrap_programmatic(window, _Handler, t, show=False)
        assert "qtcnc.dro_x.value-out" in result.hal

    def test_widgets_are_wired(self):
        window, (estop, power, dro, state) = _build_window()
        t = MockTransport()
        bootstrap_programmatic(window, _Handler, t, show=False)
        # Initial estop state should seed the estop button label.
        assert estop.text() == "ESTOP RESET"
        assert state.text() == "ESTOPPED"
        assert power.isEnabled() is False

    def test_handler_on_estop_auto_connected(self):
        window, _ = _build_window()
        t = MockTransport()
        result = bootstrap_programmatic(window, _Handler, t, show=False)
        t.exec_command(CommandVerb.ESTOP_RESET)
        QApplication.processEvents()
        assert "on_estop:False" in result.handler.seen

    def test_signal_drives_widget_after_bootstrap(self):
        window, (estop, power, dro, state) = _build_window()
        t = MockTransport()
        bootstrap_programmatic(window, _Handler, t, show=False)
        t.mutate_state(position=Position(x=5.0))
        QApplication.processEvents()
        assert "5.0000" in dro.text()
        assert t.pin_value("qtcnc.dro_x.value-out") == 5.0

    def test_handler_extra_pins_get_declared(self):
        window, _ = _build_window()
        t = MockTransport()

        class _HandlerWithExtras(_Handler):
            def declare_pins(self) -> list[HalPinSpec]:
                return [
                    HalPinSpec(
                        name="qtcnc.handler.flag",
                        type=HalType.BIT, dir=HalDir.OUT,
                    ),
                ]

        result = bootstrap_programmatic(window, _HandlerWithExtras, t, show=False)
        assert "qtcnc.handler.flag" in result.created_pins

    def test_handler_pin_collision_with_widget_raises(self):
        window, _ = _build_window()
        t = MockTransport()

        class _HandlerCollision(_Handler):
            def declare_pins(self) -> list[HalPinSpec]:
                return [
                    HalPinSpec(
                        name="qtcnc.dro_x.value-out",  # same as DRO
                        type=HalType.FLOAT, dir=HalDir.OUT,
                    ),
                ]

        with pytest.raises(ValueError):
            bootstrap_programmatic(window, _HandlerCollision, t, show=False)

    def test_reconnector_wired_with_initial_id(self):
        window, _ = _build_window()
        t = MockTransport()
        result = bootstrap_programmatic(window, _Handler, t, show=False)
        # Reconnector exists and is exposed on both result and window.
        assert result.reconnector is window.qtcnc_reconnector
        # Initial daemon_instance_id from MockTransport's hello() should be
        # captured so daemon_restarted detection works on the first reconnect.
        assert result.reconnector.daemon_instance_id == t._daemon_instance_id
        # Not started by bootstrap; CLI main() is responsible for starting.
        assert not result.reconnector._ping_timer.isActive()

    def test_reconnector_disconnect_routes_through_status(self):
        window, _ = _build_window()
        t = MockTransport()
        result = bootstrap_programmatic(window, _Handler, t, show=False)
        disconnects: list[str] = []
        result.status.disconnected.connect(lambda r: disconnects.append(r))
        result.reconnector._handle_disconnect("test")
        assert disconnects == ["link lost"]


# ---------------------------------------------------------------------------
# Screen resolution
# ---------------------------------------------------------------------------


class TestResolveScreenDir:
    def test_resolves_from_env_path(self, tmp_path, monkeypatch):
        screen = tmp_path / "myscreen"
        screen.mkdir()
        (screen / "main.ui").write_text("<ui/>")
        (screen / "handler.py").write_text("from qtcnc.handler import QtcncHandler\n")
        monkeypatch.setenv("QTCNC_SCREEN_PATH", str(tmp_path))
        path = _resolve_screen_dir("myscreen")
        assert path == screen

    def test_missing_screen_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QTCNC_SCREEN_PATH", str(tmp_path))
        with pytest.raises(FileNotFoundError):
            _resolve_screen_dir("nope")


# ---------------------------------------------------------------------------
# Standard screen smoke test — loads the shipped `standard` screen through
# the full bootstrap sequence to catch breakage in the .ui file, widget
# auto-wiring, or handler imports. The gcode C extension is required for
# GcodePreview, so gate on it.
# ---------------------------------------------------------------------------


class TestStandardScreen:
    @pytest.fixture(autouse=True)
    def _require_gcode(self):
        pytest.importorskip("gcode")

    def _load_standard(self):
        from qtpy import uic
        screen_dir = _resolve_screen_dir("standard")
        handler_cls = _load_handler_class(screen_dir / "handler.py")
        window = uic.loadUi(str(screen_dir / "main.ui"))
        if not isinstance(window, QMainWindow):
            mw = QMainWindow()
            mw.setCentralWidget(window)
            window = mw
        transport = MockTransport()
        result = bootstrap_programmatic(
            window, handler_cls, transport,
            config=QtcncConfig.mock_default(), show=False,
        )
        return transport, result

    def test_bootstrap_loads_every_widget(self):
        _, result = self._load_standard()
        names = sorted(w.objectName() for w in result.widgets)
        # Every named custom widget in main.ui should show up.
        expected = [
            "dro_grid", "estop_btn", "feed_override", "file_open_btn",
            "gcode_preview", "gcode_view", "jog_pad", "power_btn",
            "rapid_override", "spindle_ctl", "spindle_override", "state_lbl",
        ]
        assert names == expected

    def test_dro_grid_declared_pins(self):
        _, result = self._load_standard()
        dro_pins = [p.name for p in result.declared_pins
                    if p.name.startswith("qtcnc.dro_grid.")]
        # Mock config is a 3-axis XYZ machine, so the grid should ask for
        # exactly three OUT pins.
        assert dro_pins == [
            "qtcnc.dro_grid.x-out",
            "qtcnc.dro_grid.y-out",
            "qtcnc.dro_grid.z-out",
        ]

    def test_program_load_reaches_gcode_widgets(self, tmp_path):
        transport, result = self._load_standard()
        path = tmp_path / "box.ngc"
        path.write_text(
            "G21 G90\nG0 X0 Y0\nG1 X10 Y0 F100\nG1 X10 Y10\n"
            "G1 X0 Y10\nG1 X0 Y0\nM2\n"
        )
        transport.load_program(str(path))
        QApplication.processEvents()
        gview = next(w for w in result.widgets
                     if type(w).__name__ == "GcodeView")
        gprev = next(w for w in result.widgets
                     if type(w).__name__ == "GcodePreview")
        assert gview.loaded_path == str(path)
        assert gprev.loaded_path == str(path)
        assert gview.toPlainText().startswith("G21 G90")
        assert gprev.segment_count > 0
