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
from qtcnc.widgets.base import QtcncWidget
from qtcnc.widgets.common.action_button import ActionButton
from qtcnc.widgets.common.state_label import StateLabel
from qtpy.QtWidgets import QLabel


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

    def test_from_ini_tool_db_path_relative(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text(textwrap.dedent("""
            [QTCNC]
            TOOL_DB = tools.db
        """).strip())
        cfg = QtcncConfig.from_ini(str(ini))
        assert cfg.tool_db_path == str(tmp_path / "tools.db")

    def test_from_ini_tool_db_path_absolute(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text(textwrap.dedent("""
            [QTCNC]
            TOOL_DB = /opt/cnc/tools.db
        """).strip())
        cfg = QtcncConfig.from_ini(str(ini))
        assert cfg.tool_db_path == "/opt/cnc/tools.db"

    def test_from_ini_no_tool_db_returns_none(self, tmp_path):
        ini = tmp_path / "minimal.ini"
        ini.write_text("[EMC]\nVERSION = 1.1\n")
        cfg = QtcncConfig.from_ini(str(ini))
        assert cfg.tool_db_path is None

    def test_from_ini_random_toolchanger(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text(textwrap.dedent("""
            [EMCIO]
            RANDOM_TOOLCHANGER = 1
        """).strip())
        cfg = QtcncConfig.from_ini(str(ini))
        assert cfg.random_toolchanger is True

    def test_from_ini_nonrandom_toolchanger_default(self, tmp_path):
        ini = tmp_path / "minimal.ini"
        ini.write_text("[EMC]\nVERSION = 1.1\n")
        cfg = QtcncConfig.from_ini(str(ini))
        assert cfg.random_toolchanger is False

    def test_mock_default_no_tool_db(self):
        cfg = QtcncConfig.mock_default()
        assert cfg.tool_db_path is None
        assert cfg.random_toolchanger is False


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


class _HalPinLabel(QtcncWidget, QLabel):
    """Test-only widget that declares one FLOAT HAL pin under its objectName.

    Kept local to the bootstrap tests so the pin-declaration, hub-registration,
    and handler-collision assertions still have a concrete widget that owns
    `qtcnc.<name>.value-out`.
    """

    HAL_PINS = [
        HalPinSpec(name="qtcnc.{name}.value-out", type=HalType.FLOAT, dir=HalDir.OUT),
    ]


def _build_window():
    window = QMainWindow()
    estop = ActionButton(window)
    estop.setObjectName("estop_btn")
    estop._set_action("estop")
    power = ActionButton(window)
    power.setObjectName("power_btn")
    power._set_action("power")
    pin = _HalPinLabel(window)
    pin.setObjectName("dro_x")
    dro = StateLabel(window)
    dro.setObjectName("dro_label")
    dro._set_state("dro_work")
    dro._set_axis(0)
    state = StateLabel(window)
    state.setObjectName("state_lbl")
    state._set_state("task_state")
    return window, (estop, power, pin, dro, state)


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
        assert window.qtcnc_messages is result.message_bus
        assert window.qtcnc_toast is not None

    def test_collects_widgets(self):
        window, widgets = _build_window()
        t = MockTransport()
        result = bootstrap_programmatic(window, _Handler, t, show=False)
        assert len(result.widgets) == len(widgets)
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
        window, (estop, power, pin, dro, state) = _build_window()
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
        t.exec_command(CommandVerb.STATE_ESTOP_RESET)
        QApplication.processEvents()
        assert "on_estop:False" in result.handler.seen

    def test_signal_drives_widget_after_bootstrap(self):
        window, (estop, power, pin, dro, state) = _build_window()
        t = MockTransport()
        bootstrap_programmatic(window, _Handler, t, show=False)
        t.mutate_state(position=Position(x=5.0))
        QApplication.processEvents()
        text = dro.text()
        assert text.startswith("X:")
        assert "5.000" in text

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
        test_bench = sorted([
            "tb_adapt_feed_en", "tb_aout", "tb_block_delete", "tb_brake",
            "tb_display_msg", "tb_dout_off", "tb_dout_on", "tb_err_msg",
            "tb_feed_hold_en", "tb_feed_ovr_en", "tb_load_tt",
            "tb_max_lim", "tb_maxvel", "tb_min_lim",
            "tb_optional_stop", "tb_override_limits",
            "tb_prog_forward", "tb_prog_reverse", "tb_reset_interp",
            "tb_set_debug", "tb_sp_const", "tb_sp_dec", "tb_sp_inc",
            "tb_spindle_ovr_en", "tb_task_synch", "tb_text_msg",
            "tb_tool_offset", "tb_traj_coord", "tb_traj_free",
            "tb_traj_teleop",
        ])
        operator_controls = sorted([
            "mode_manual_btn", "mode_auto_btn", "mode_mdi_btn",
            "jog_x_inc_minus", "jog_x_inc_plus",
            "jog_z_inc_minus", "jog_z_inc_plus",
            "home_all_btn",
            "home_x_btn", "home_y_btn", "home_z_btn",
            "program_run_btn", "program_pause_btn", "program_resume_btn",
            "program_stop_btn", "program_step_btn",
            "mdi_btn",
            "mdi_entry", "mdi_run_btn",
            "mist_btn", "flood_btn",
        ])
        expected = sorted([
            "dro_grid", "estop_btn", "feed_override", "file_open_btn",
            "gcode_preview", "gcode_view",
            "jog_x_minus", "jog_x_plus",
            "jog_y_minus", "jog_y_plus",
            "jog_z_minus", "jog_z_plus",
            "message_log", "message_status_bar",
            "power_btn",
            "rapid_override",
            "spindle_ccw", "spindle_cw", "spindle_override", "spindle_stop",
            "state_lbl",
            "tool_offset_view",
            *operator_controls,
            *test_bench,
        ])
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
