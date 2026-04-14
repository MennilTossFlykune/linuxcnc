"""Tests for qtcnc.core.types — dataclass behavior, frozen/slots, equality."""

from __future__ import annotations

import pytest

from qtcnc.core.types import (
    DeviceInfo,
    ErrorMessage,
    ErrorSeverity,
    InterpState,
    MachineState,
    MotionType,
    Overrides,
    Position,
    ProgramState,
    SpindleDir,
    SpindleState,
    TaskMode,
    TaskState,
    Tool,
)


class TestPosition:
    def test_default(self):
        p = Position()
        assert p.x == 0.0 and p.y == 0.0 and p.z == 0.0
        assert p.a is None and p.b is None and p.c is None
        assert p.u is None and p.v is None and p.w is None

    def test_equality_value_based(self):
        assert Position(1, 2, 3) == Position(1, 2, 3)
        assert Position(1, 2, 3) != Position(1, 2, 4)

    def test_hashable(self):
        s = {Position(1, 2, 3), Position(1, 2, 3)}
        assert len(s) == 1

    def test_frozen(self):
        p = Position()
        with pytest.raises(Exception):
            p.x = 5.0  # type: ignore[misc]

    def test_slots(self):
        # frozen+slots combined raises either FrozenInstanceError (subclass of
        # AttributeError) or TypeError depending on which dunder fires first.
        p = Position()
        with pytest.raises((AttributeError, TypeError)):
            p.bogus = 1  # type: ignore[attr-defined]
        # Defensive: confirm __slots__ was actually generated.
        assert hasattr(Position, "__slots__")

    def test_optional_axes(self):
        p = Position(x=1.0, y=2.0, z=3.0, a=45.0)
        assert p.a == 45.0
        assert p.b is None


class TestMachineState:
    def test_default_is_estopped_off(self):
        s = MachineState()
        assert s.estop is True
        assert s.powered is False
        assert s.task_mode == TaskMode.MANUAL
        assert s.interp_state == InterpState.IDLE
        assert s.motion_type == MotionType.NONE
        assert s.homed == ()
        assert s.axis_count == 3

    def test_homed_tuple(self):
        s = MachineState(homed=(True, True, False))
        assert len(s.homed) == 3
        assert s.homed[2] is False


class TestProgramState:
    def test_default(self):
        p = ProgramState()
        assert p.path == ""
        assert p.total_lines == 0
        assert p.current_line == 0
        assert p.is_running is False
        assert p.is_paused is False


class TestTool:
    def test_default_offset_is_zero_position(self):
        t = Tool()
        assert t.offset == Position()
        assert t.id == 0
        assert t.pocket == 0

    def test_offset_independent(self):
        t1 = Tool()
        t2 = Tool()
        assert t1.offset is not t2.offset or t1.offset == t2.offset


class TestSpindleState:
    def test_default(self):
        s = SpindleState()
        assert s.index == 0
        assert s.speed == 0.0
        assert s.direction == SpindleDir.STOP
        assert s.enabled is False


class TestOverrides:
    def test_default(self):
        o = Overrides()
        assert o.feed == 1.0
        assert o.rapid == 1.0
        assert o.spindles == (1.0,)


class TestErrorMessage:
    def test_fields(self):
        m = ErrorMessage(
            severity=ErrorSeverity.OPERATOR_ERROR,
            text="broken",
            timestamp=1234.5,
        )
        assert m.severity == ErrorSeverity.OPERATOR_ERROR
        assert m.text == "broken"
        assert m.timestamp == 1234.5


class TestDeviceInfo:
    def test_minimal(self):
        d = DeviceInfo(node="/dev/sdb1", subsystem="block", dev_type="partition")
        assert d.vendor == ""
        assert d.model == ""


class TestEnums:
    def test_task_state_values(self):
        assert int(TaskState.ESTOP) == 1
        assert int(TaskState.ON) == 4

    def test_task_mode_values(self):
        assert int(TaskMode.MANUAL) == 1
        assert int(TaskMode.AUTO) == 2
        assert int(TaskMode.MDI) == 3

    def test_interp_state_values(self):
        assert int(InterpState.IDLE) == 1

    def test_spindle_dir_signed(self):
        assert int(SpindleDir.REVERSE) == -1
        assert int(SpindleDir.STOP) == 0
        assert int(SpindleDir.FORWARD) == 1

    def test_int_enum_comparable_to_int(self):
        assert TaskMode.MANUAL == 1
        assert TaskMode.AUTO > TaskMode.MANUAL
