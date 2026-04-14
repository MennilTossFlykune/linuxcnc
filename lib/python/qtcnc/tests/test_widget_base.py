"""Tests for qtcnc.widgets.base and qtcnc.widgets.hal."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from qtpy.QtCore import QObject
from qtpy.QtWidgets import QApplication, QMainWindow, QWidget

from qtcnc.core.command import Command
from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType
from qtcnc.core.status import Status
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.base import PinCollision, QtcncWidget, collect_pin_declarations
from qtcnc.widgets.hal import HalPin, HalPinHub


@pytest.fixture(scope="session", autouse=True)
def qapp():
    """Provide a QApplication for the whole test session."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class _DroLike(QtcncWidget, QWidget):
    HAL_PINS = [
        HalPinSpec(name="qtcnc.{name}.value-out", type=HalType.FLOAT, dir=HalDir.OUT),
    ]


class _ButtonLike(QtcncWidget, QWidget):
    HAL_PINS = [
        HalPinSpec(name="qtcnc.{name}.pressed", type=HalType.BIT, dir=HalDir.OUT),
        HalPinSpec(name="qtcnc.{name}.led", type=HalType.BIT, dir=HalDir.IN),
    ]


class _StaticPin(QtcncWidget, QWidget):
    HAL_PINS = [
        HalPinSpec(name="qtcnc.global.probe-enabled", type=HalType.BIT, dir=HalDir.OUT),
    ]


class _NoPins(QtcncWidget, QWidget):
    pass


# ---------------------------------------------------------------------------
# HAL_PINS templating
# ---------------------------------------------------------------------------


class TestDeclaredPins:
    def test_single_templated_pin(self):
        w = _DroLike()
        w.setObjectName("dro_x")
        specs = _DroLike.declared_pins(w)
        assert len(specs) == 1
        assert specs[0].name == "qtcnc.dro_x.value-out"
        assert specs[0].owner_widget == "dro_x"

    def test_multiple_templated_pins(self):
        w = _ButtonLike()
        w.setObjectName("estop")
        specs = _ButtonLike.declared_pins(w)
        names = [s.name for s in specs]
        assert "qtcnc.estop.pressed" in names
        assert "qtcnc.estop.led" in names

    def test_static_pin_name_unchanged(self):
        w = _StaticPin()
        w.setObjectName("probe_switch")
        specs = _StaticPin.declared_pins(w)
        assert specs[0].name == "qtcnc.global.probe-enabled"

    def test_empty_pins(self):
        w = _NoPins()
        w.setObjectName("blank")
        assert _NoPins.declared_pins(w) == []

    def test_missing_object_name_raises(self):
        w = _DroLike()
        with pytest.raises(ValueError):
            _DroLike.declared_pins(w)


# ---------------------------------------------------------------------------
# collect_pin_declarations
# ---------------------------------------------------------------------------


class TestCollectPinDeclarations:
    def test_collects_from_mixed_list(self):
        dro = _DroLike(); dro.setObjectName("dro_x")
        btn = _ButtonLike(); btn.setObjectName("estop")
        specs = collect_pin_declarations([dro, btn])
        names = {s.name for s in specs}
        assert names == {
            "qtcnc.dro_x.value-out",
            "qtcnc.estop.pressed",
            "qtcnc.estop.led",
        }

    def test_collision_on_same_static_pin(self):
        a = _StaticPin(); a.setObjectName("a")
        b = _StaticPin(); b.setObjectName("b")
        with pytest.raises(PinCollision):
            collect_pin_declarations([a, b])

    def test_collision_on_same_templated_pin(self):
        d1 = _DroLike(); d1.setObjectName("dro_x")
        d2 = _DroLike(); d2.setObjectName("dro_x")  # same object name
        with pytest.raises(PinCollision):
            collect_pin_declarations([d1, d2])

    def test_no_collision_on_different_templated_names(self):
        dx = _DroLike(); dx.setObjectName("dro_x")
        dy = _DroLike(); dy.setObjectName("dro_y")
        specs = collect_pin_declarations([dx, dy])
        assert len(specs) == 2


# ---------------------------------------------------------------------------
# HalPin / HalPinHub
# ---------------------------------------------------------------------------


class TestHalPinHub:
    def test_get_or_create_returns_same_instance(self):
        t = MockTransport()
        hub = HalPinHub(t)
        pin_a = hub.get_or_create("qtcnc.a")
        pin_b = hub.get_or_create("qtcnc.a")
        assert pin_a is pin_b

    def test_contains(self):
        t = MockTransport()
        hub = HalPinHub(t)
        hub.get_or_create("qtcnc.a")
        assert "qtcnc.a" in hub
        assert "qtcnc.b" not in hub

    def test_update_fires_signal(self):
        t = MockTransport()
        hub = HalPinHub(t)
        pin = hub.get_or_create("qtcnc.a")
        values: list[Any] = []
        pin.value_changed.connect(values.append)
        t.mutate_pin("qtcnc.a", 3.14)
        assert values == [3.14]
        assert pin.value == 3.14

    def test_update_ignored_for_unregistered_pin(self):
        t = MockTransport()
        hub = HalPinHub(t)
        # Should not crash; unregistered pins are silently dropped.
        t.mutate_pin("ghost.pin", 1)

    def test_write_forwards_to_transport(self):
        t = MockTransport()
        hub = HalPinHub(t)
        t.declare_pins([
            HalPinSpec(name="qtcnc.a", type=HalType.FLOAT, dir=HalDir.OUT),
        ])
        pin = hub.get_or_create("qtcnc.a")
        pin.set(1.5)
        assert t.pin_value("qtcnc.a") == 1.5

    def test_write_via_hub_fires_its_own_signal(self):
        """A write() round-trips through the mock transport and fires
        the update callback, which then fires the pin signal."""
        t = MockTransport()
        hub = HalPinHub(t)
        t.declare_pins([
            HalPinSpec(name="qtcnc.b", type=HalType.FLOAT, dir=HalDir.OUT),
        ])
        pin = hub.get_or_create("qtcnc.b")
        values: list[Any] = []
        pin.value_changed.connect(values.append)
        pin.set(2.0)
        assert values == [2.0]


# ---------------------------------------------------------------------------
# QtcncWidget instance helpers
# ---------------------------------------------------------------------------


class _Window(QMainWindow):
    """Stand-in for a real bootstrap window with qtcnc_status / qtcnc_hal attached."""

    def __init__(self, status: Status, hub: HalPinHub) -> None:
        super().__init__()
        self.qtcnc_status = status
        self.qtcnc_hal = hub


class TestConnectStatusAndPinLookup:
    def test_connect_status_routes_signal(self):
        t = MockTransport()
        status = Status(t)
        hub = HalPinHub(t)
        win = _Window(status, hub)
        dro = _DroLike(win)
        dro.setObjectName("dro_x")
        seen: list[Any] = []
        dro.connect_status("position_changed", seen.append)
        t.hello()
        from qtcnc.core.types import Position
        t.mutate_state(position=Position(x=1.0))
        # Qt.QueuedConnection requires the event loop to tick.
        QApplication.processEvents()
        assert len(seen) == 1

    def test_connect_status_unknown_signal(self):
        t = MockTransport()
        status = Status(t)
        hub = HalPinHub(t)
        win = _Window(status, hub)
        dro = _DroLike(win)
        dro.setObjectName("dro_x")
        with pytest.raises(AttributeError):
            dro.connect_status("nonexistent_signal", lambda: None)

    def test_pin_lookup(self):
        t = MockTransport()
        status = Status(t)
        hub = HalPinHub(t)
        t.declare_pins([
            HalPinSpec(name="qtcnc.dro_x.value-out", type=HalType.FLOAT, dir=HalDir.OUT),
        ])
        hub.get_or_create("qtcnc.dro_x.value-out")
        win = _Window(status, hub)
        dro = _DroLike(win)
        dro.setObjectName("dro_x")
        pin = dro.pin("value-out")
        assert isinstance(pin, HalPin)
        assert pin.name == "qtcnc.dro_x.value-out"

    def test_pin_lookup_missing(self):
        t = MockTransport()
        status = Status(t)
        hub = HalPinHub(t)
        win = _Window(status, hub)
        dro = _DroLike(win)
        dro.setObjectName("dro_x")
        with pytest.raises(KeyError):
            dro.pin("value-out")

    def test_bare_widget_without_bootstrap_raises(self):
        # No window attached → _status() and _hal_hub() should raise.
        dro = _DroLike()
        dro.setObjectName("dro_x")
        with pytest.raises(RuntimeError):
            dro.connect_status("position_changed", lambda p: None)
