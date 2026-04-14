"""Tests for qtcnc.core.hal_spec — HalPinSpec and {name} substitution."""

from __future__ import annotations

import pytest

from qtcnc.core.hal_spec import HalDir, HalPinSpec, HalType


class TestHalPinSpec:
    def test_construct(self):
        s = HalPinSpec(name="qtcnc.foo", type=HalType.BIT, dir=HalDir.OUT)
        assert s.name == "qtcnc.foo"
        assert s.type == HalType.BIT
        assert s.dir == HalDir.OUT
        assert s.initial is None
        assert s.owner_widget is None

    def test_equality(self):
        a = HalPinSpec("qtcnc.a", HalType.FLOAT, HalDir.OUT)
        b = HalPinSpec("qtcnc.a", HalType.FLOAT, HalDir.OUT)
        c = HalPinSpec("qtcnc.b", HalType.FLOAT, HalDir.OUT)
        assert a == b
        assert a != c

    def test_hashable(self):
        a = HalPinSpec("qtcnc.a", HalType.BIT, HalDir.IN)
        assert len({a, a}) == 1

    def test_frozen(self):
        s = HalPinSpec("qtcnc.a", HalType.BIT, HalDir.IN)
        with pytest.raises((AttributeError, TypeError)):
            s.name = "other"  # type: ignore[misc]

    def test_substitute_replaces_name(self):
        tpl = HalPinSpec("qtcnc.{name}.value-out", HalType.FLOAT, HalDir.OUT)
        out = tpl.substitute("dro_x")
        assert out.name == "qtcnc.dro_x.value-out"
        assert out.type == HalType.FLOAT
        assert out.dir == HalDir.OUT
        assert out.owner_widget == "dro_x"

    def test_substitute_returns_self_without_placeholder(self):
        tpl = HalPinSpec("qtcnc.explicit.name", HalType.BIT, HalDir.OUT)
        out = tpl.substitute("whatever")
        assert out is tpl  # unchanged, no new allocation

    def test_substitute_preserves_other_fields(self):
        tpl = HalPinSpec(
            name="qtcnc.{name}.pin",
            type=HalType.S32,
            dir=HalDir.IN,
            initial=42,
        )
        out = tpl.substitute("sensor")
        assert out.initial == 42
        assert out.type == HalType.S32

    def test_substitute_missing_key_raises(self):
        tpl = HalPinSpec("qtcnc.{bogus}.pin", HalType.BIT, HalDir.OUT)
        with pytest.raises(KeyError):
            tpl.substitute("foo")


class TestHalEnums:
    def test_hal_type_values(self):
        assert int(HalType.BIT) == 1
        assert int(HalType.FLOAT) == 2
        assert int(HalType.S32) == 3
        assert int(HalType.U32) == 4

    def test_hal_dir_values(self):
        assert int(HalDir.IN) == 1
        assert int(HalDir.OUT) == 2
        assert int(HalDir.IO) == 3
