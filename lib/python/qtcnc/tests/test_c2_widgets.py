"""C2 widget tests: OverrideSlider in mock mode."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.core.command import Command
from qtcnc.core.status import Status
from qtcnc.core.types import MachineState, Overrides, SpindleDir, SpindleState, TaskMode
from qtcnc.signals import CommandVerb
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.base import QtcncWidget, collect_pin_declarations
from qtcnc.widgets.common.override_slider import OverrideKind, OverrideSlider
from qtcnc.widgets.hal import HalPinHub


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class _RecordingMock(MockTransport):
    """MockTransport that captures every exec_command for inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[CommandVerb, dict[str, Any]]] = []

    def exec_command(self, verb: CommandVerb, **kwargs: Any) -> None:
        self.commands.append((verb, kwargs))
        super().exec_command(verb, **kwargs)


def _make_world(*widget_factories, transport: MockTransport | None = None):
    t = transport if transport is not None else _RecordingMock()
    window = QMainWindow()
    widgets: list[QtcncWidget] = [f(window) for f in widget_factories]
    status = Status(t)
    hub = HalPinHub(t)
    cmd = Command(t)
    window.qtcnc_status = status
    window.qtcnc_command = cmd
    window.qtcnc_hal = hub
    t.hello()
    status.bootstrap()
    specs = collect_pin_declarations(widgets)
    if specs:
        result = t.declare_pins(specs)
        for name in result.created:
            hub.get_or_create(name)
    for w in widgets:
        w.qtcnc_setup()
    return t, status, cmd, window, widgets


# ---------------------------------------------------------------------------
# OverrideSlider
# ---------------------------------------------------------------------------


class TestOverrideSlider:
    def _mk_feed(self):
        def factory(window):
            s = OverrideSlider(window)
            s.setObjectName("feed_slider")
            s._set_kind("feed")
            return s
        return _make_world(factory)

    def _mk_rapid(self):
        def factory(window):
            s = OverrideSlider(window)
            s.setObjectName("rapid_slider")
            s._set_kind("rapid")
            return s
        return _make_world(factory)

    def _mk_spindle(self, index: int = 0):
        def factory(window):
            s = OverrideSlider(window)
            s.setObjectName(f"spindle_slider_{index}")
            s._set_kind("spindle")
            s._set_index(index)
            return s
        return _make_world(factory)

    def test_initial_label_is_100_percent(self):
        t, status, cmd, win, (slider,) = self._mk_feed()
        assert slider._label.text() == "100%"

    def test_kind_property_is_enum(self):
        t, status, cmd, win, (slider,) = self._mk_feed()
        assert slider.kind == OverrideKind.feed
        assert slider.kind.name == "feed"

    def test_kind_accepts_enum_instance(self):
        s = OverrideSlider()
        s._set_kind(OverrideKind.spindle)
        assert s.kind is OverrideKind.spindle

    def test_drag_emits_feedrate(self):
        t, status, cmd, win, (slider,) = self._mk_feed()
        slider._slider.setValue(750)
        QApplication.processEvents()
        feed_cmds = [(v, kw) for v, kw in t.commands
                     if v == CommandVerb.FEEDRATE]
        assert feed_cmds[-1][1] == {"value": 0.75}
        assert slider._label.text() == "75%"

    def test_external_state_update_does_not_re_emit(self):
        t, status, cmd, win, (slider,) = self._mk_feed()
        t.mutate_state(overrides=Overrides(feed=0.5, rapid=1.0, spindles=(1.0,)))
        QApplication.processEvents()
        assert slider._slider.value() == 500
        feed_cmds = [v for v, _ in t.commands if v == CommandVerb.FEEDRATE]
        assert feed_cmds == []

    def test_rapid_slider_routes_to_rapidrate(self):
        t, status, cmd, win, (slider,) = self._mk_rapid()
        slider._slider.setValue(250)
        rapid_cmds = [(v, kw) for v, kw in t.commands
                      if v == CommandVerb.RAPIDRATE]
        assert rapid_cmds[-1][1] == {"value": 0.25}

    def test_spindle_slider_routes_to_spindleoverride(self):
        t, status, cmd, win, (slider,) = self._mk_spindle(index=0)
        slider._slider.setValue(900)
        cmds = [(v, kw) for v, kw in t.commands
                if v == CommandVerb.SPINDLEOVERRIDE]
        assert cmds[-1][1] == {"index": 0, "value": 0.9}

    def test_spindle_slider_ignores_other_index(self):
        t, status, cmd, win, (slider,) = self._mk_spindle(index=0)
        t.mutate_state(overrides=Overrides(
            feed=1.0, rapid=1.0, spindles=(1.0, 0.4),
        ))
        QApplication.processEvents()
        assert slider._slider.value() == 1000

    def test_max_value_property_extends_range(self):
        t, status, cmd, win, (slider,) = self._mk_feed()
        slider._set_max(2.0)
        assert slider._slider.maximum() == 2000

    def test_unknown_kind_raises(self):
        s = OverrideSlider()
        with pytest.raises(ValueError):
            s._set_kind("acceleration")
