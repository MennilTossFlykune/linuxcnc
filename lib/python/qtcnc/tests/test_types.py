"""Tests for qtcnc.core.types — dataclass behavior, frozen/slots, equality."""

from __future__ import annotations

import pytest

from qtcnc.core.types import (
    AxisState,
    CoolantState,
    DeviceInfo,
    ErrorMessage,
    ErrorSeverity,
    ExecState,
    InterpSettings,
    InterpState,
    IoState,
    JointState,
    LinearUnits,
    MachineState,
    MotionMode,
    MotionType,
    Offsets,
    Overrides,
    Position,
    ProbeState,
    ProgramState,
    ProgramUnits,
    SpindleDir,
    SpindleState,
    TaskInfo,
    TaskMode,
    TaskState,
    Tool,
    ToolEntry,
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
        assert s.motion_mode == MotionMode.FREE
        assert s.kinematics_identity is False
        assert s.linear_units == LinearUnits.MM
        assert s.axis_mask == 0b111

    def test_default_topology_counts(self):
        s = MachineState()
        assert s.joint_count == 0
        assert s.spindle_count == 0
        assert s.num_extrajoints == 0

    def test_default_kinematics_and_units(self):
        s = MachineState()
        assert s.cycle_time == 0.0
        assert s.linear_units_per_mm == 1.0
        assert s.angular_units_per_deg == 1.0
        assert s.kinematics_type == 1

    def test_default_motion_runtime(self):
        s = MachineState()
        assert s.motion_enabled is False
        assert s.inpos is False
        assert s.queue == 0
        assert s.active_queue == 0
        assert s.queue_full is False
        assert s.motion_id == 0
        assert s.single_stepping is False
        assert s.commanded_velocity == 0.0
        assert s.commanded_acceleration == 0.0
        assert s.max_acceleration == 0.0
        assert s.distance_to_go_scalar == 0.0

    def test_homed_tuple(self):
        s = MachineState(homed=(True, True, False))
        assert len(s.homed) == 3
        assert s.homed[2] is False

    def test_axis_letters_xyz(self):
        assert MachineState(axis_mask=0b111).axis_letters == ("X", "Y", "Z")

    def test_axis_letters_gantry_is_unique(self):
        # XYYZ (four-joint gantry) still has three unique axes.
        s = MachineState(axis_mask=0b111, homed=(True, True, True, True))
        assert s.axis_letters == ("X", "Y", "Z")
        assert s.num_axes == 3

    def test_axis_letters_skips_gaps(self):
        # XZA: mask bits 0, 2, 3 -> letters X, Z, A
        s = MachineState(axis_mask=(1 << 0) | (1 << 2) | (1 << 3))
        assert s.axis_letters == ("X", "Z", "A")
        assert s.num_axes == 3

    def test_linear_units_enum_values(self):
        assert int(LinearUnits.MM) == 1
        assert int(LinearUnits.INCH) == 2

    def test_coordinates_default_empty(self):
        assert MachineState().coordinates == ()

    def test_axis_for_joint_with_coordinates(self):
        m = MachineState(coordinates=("X", "Y", "Y", "Z"))
        assert m.axis_for_joint(0) == 0  # X
        assert m.axis_for_joint(1) == 1  # first Y
        assert m.axis_for_joint(2) == 1  # second Y
        assert m.axis_for_joint(3) == 2  # Z

    def test_axis_for_joint_identity_fallback(self):
        m = MachineState(coordinates=())
        assert m.axis_for_joint(0) == 0
        assert m.axis_for_joint(2) == 2

    def test_joint_for_axis_with_coordinates(self):
        m = MachineState(coordinates=("X", "Y", "Y", "Z"))
        assert m.joint_for_axis(0) == 0  # X → joint 0
        assert m.joint_for_axis(1) == 1  # Y → first Y joint
        assert m.joint_for_axis(2) == 3  # Z → joint 3

    def test_joint_for_axis_identity_fallback(self):
        m = MachineState(coordinates=())
        assert m.joint_for_axis(0) == 0
        assert m.joint_for_axis(2) == 2

    def test_joints_for_axis_gantry(self):
        m = MachineState(coordinates=("X", "Y", "Y", "Z"))
        assert m.joints_for_axis(0) == (0,)
        assert m.joints_for_axis(1) == (1, 2)
        assert m.joints_for_axis(2) == (3,)

    def test_joints_for_axis_identity_fallback(self):
        m = MachineState(coordinates=())
        assert m.joints_for_axis(0) == (0,)
        assert m.joints_for_axis(2) == (2,)

    def test_axis_for_joint_rotary(self):
        m = MachineState(coordinates=("X", "Y", "Z", "A"))
        assert m.axis_for_joint(3) == 3  # A is index 3


class TestProgramState:
    def test_default(self):
        p = ProgramState()
        assert p.path == ""
        assert p.total_lines == 0
        assert p.current_line == 0
        assert p.is_running is False
        assert p.is_paused is False
        assert p.motion_line == 0
        assert p.program_units == ProgramUnits.MM


class TestTool:
    def test_default_offset_is_zero_position(self):
        t = Tool()
        assert t.offset == Position()
        assert t.id == 0
        assert t.pocket == 0
        assert t.frontangle == 0.0
        assert t.backangle == 0.0
        assert t.orientation == 0

    def test_offset_independent(self):
        t1 = Tool()
        t2 = Tool()
        assert t1.offset is not t2.offset or t1.offset == t2.offset

    def test_set_geometry_fields(self):
        t = Tool(id=3, frontangle=45.0, backangle=30.0, orientation=6)
        assert t.frontangle == 45.0
        assert t.backangle == 30.0
        assert t.orientation == 6


class TestSpindleState:
    def test_default(self):
        s = SpindleState()
        assert s.index == 0
        assert s.speed == 0.0
        assert s.direction == SpindleDir.STOP
        assert s.enabled is False
        assert s.brake is False
        assert s.override_enabled is True
        assert s.homed is False
        assert s.orient_state == 0
        assert s.orient_fault == 0


class TestOverrides:
    def test_default(self):
        o = Overrides()
        assert o.feed == 1.0
        assert o.rapid == 1.0
        assert o.spindles == (1.0,)
        assert o.max_velocity == 0.0
        assert o.feed_enabled is True
        assert o.adaptive_enabled is False
        assert o.hold_enabled is True


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

    def test_exec_state_values(self):
        assert int(ExecState.DONE) == 2
        assert int(ExecState.WAITING_FOR_MOTION) == 3
        assert int(ExecState.WAITING_FOR_SPINDLE_ORIENTED) == 10

    def test_program_units_values(self):
        assert int(ProgramUnits.INCH) == 1
        assert int(ProgramUnits.MM) == 2
        assert int(ProgramUnits.CM) == 3


class TestTaskInfo:
    def test_default(self):
        t = TaskInfo()
        assert t.rcs_state == 0
        assert t.echo_serial_number == 0
        assert t.exec_state == ExecState.DONE
        assert t.call_level == 0
        assert t.current_line == 0
        assert t.active_command_line == ""
        assert t.interpreter_errcode == 0
        assert t.optional_stop is False
        assert t.block_delete is False
        assert t.task_paused == 0
        assert t.input_timeout is False
        assert t.ini_filename == ""
        assert t.delay_left == 0.0
        assert t.queued_mdi_commands == 0
        assert t.debug_mask == 0

    def test_set_fields(self):
        t = TaskInfo(
            rcs_state=3,
            exec_state=ExecState.WAITING_FOR_IO,
            active_command_line="G1 X10",
            optional_stop=True,
            ini_filename="/tmp/foo.ini",
            debug_mask=0x7F,
        )
        assert t.rcs_state == 3
        assert t.exec_state == ExecState.WAITING_FOR_IO
        assert t.active_command_line == "G1 X10"
        assert t.optional_stop is True
        assert t.ini_filename == "/tmp/foo.ini"
        assert t.debug_mask == 0x7F


class TestOffsets:
    def test_default(self):
        o = Offsets()
        assert o.g5x_index == 1
        assert o.g5x == Position()
        assert o.g92 == Position()
        assert o.tool_offset == Position()
        assert o.rotation_xy == 0.0

    def test_set_fields(self):
        o = Offsets(g5x_index=2, g5x=Position(x=1.0), rotation_xy=45.0)
        assert o.g5x_index == 2
        assert o.g5x.x == 1.0
        assert o.rotation_xy == 45.0


class TestInterpSettings:
    def test_default(self):
        s = InterpSettings()
        assert s.sequence_number == 0.0
        assert s.feed == 0.0
        assert s.speed == 0.0
        assert s.g64_blend_tolerance == 0.0
        assert s.naive_cam_tolerance == 0.0

    def test_set_fields(self):
        s = InterpSettings(
            sequence_number=123.0, feed=100.0, speed=2000.0,
            g64_blend_tolerance=0.01, naive_cam_tolerance=0.02,
        )
        assert s.sequence_number == 123.0
        assert s.feed == 100.0
        assert s.speed == 2000.0
        assert s.g64_blend_tolerance == 0.01
        assert s.naive_cam_tolerance == 0.02


class TestJointState:
    def test_default(self):
        j = JointState()
        assert j.joint_type == 0
        assert j.units == 1.0
        assert j.backlash == 0.0
        assert j.min_position_limit == 0.0
        assert j.max_position_limit == 0.0
        assert j.max_ferror == 0.0
        assert j.min_ferror == 0.0
        assert j.ferror_current == 0.0
        assert j.ferror_highmark == 0.0
        assert j.output == 0.0
        assert j.input == 0.0
        assert j.velocity == 0.0
        assert j.inpos is False
        assert j.homing_state == 0
        assert j.homed is False
        assert j.fault is False
        assert j.enabled is False
        assert j.min_soft_limit is False
        assert j.max_soft_limit is False
        assert j.min_hard_limit is False
        assert j.max_hard_limit is False
        assert j.override_limits is False

    def test_set_fields(self):
        j = JointState(
            joint_type=1, units=0.001, min_position_limit=-100.0,
            max_position_limit=100.0, output=12.5, input=12.4,
            velocity=3.0, inpos=True, homed=True, enabled=True,
        )
        assert j.joint_type == 1
        assert j.units == 0.001
        assert j.min_position_limit == -100.0
        assert j.max_position_limit == 100.0
        assert j.output == 12.5
        assert j.input == 12.4
        assert j.velocity == 3.0
        assert j.inpos is True
        assert j.homed is True
        assert j.enabled is True


class TestAxisState:
    def test_default(self):
        a = AxisState()
        assert a.velocity == 0.0
        assert a.min_position_limit == 0.0
        assert a.max_position_limit == 0.0

    def test_set_fields(self):
        a = AxisState(velocity=5.0, min_position_limit=-200.0, max_position_limit=200.0)
        assert a.velocity == 5.0
        assert a.min_position_limit == -200.0
        assert a.max_position_limit == 200.0


class TestToolEntry:
    def test_default(self):
        t = ToolEntry()
        assert t.id == 0
        assert t.offset == Position()
        assert t.diameter == 0.0
        assert t.frontangle == 0.0
        assert t.backangle == 0.0
        assert t.orientation == 0

    def test_set_fields(self):
        t = ToolEntry(
            id=3, offset=Position(x=0.0, y=0.0, z=5.0),
            diameter=6.0, frontangle=45.0, backangle=30.0, orientation=6,
        )
        assert t.id == 3
        assert t.offset.z == 5.0
        assert t.diameter == 6.0
        assert t.orientation == 6


class TestCoolantState:
    def test_default(self):
        c = CoolantState()
        assert c.mist is False
        assert c.flood is False

    def test_set_fields(self):
        c = CoolantState(mist=True, flood=True)
        assert c.mist is True
        assert c.flood is True


class TestProbeState:
    def test_default(self):
        p = ProbeState()
        assert p.tripped is False
        assert p.probing is False
        assert p.value == 0
        assert p.probed_position == Position()

    def test_set_fields(self):
        p = ProbeState(
            tripped=True, probing=True, value=1,
            probed_position=Position(x=1.0, y=2.0, z=3.0),
        )
        assert p.tripped is True
        assert p.probing is True
        assert p.value == 1
        assert p.probed_position.x == 1.0


class TestIoState:
    def test_default(self):
        io = IoState()
        assert io.digital_in == ()
        assert io.digital_out == ()
        assert io.analog_in == ()
        assert io.analog_out == ()
        assert io.misc_error == ()
        assert io.pocket_prepped == -1
        assert io.tool_from_pocket == 0
        assert io.aux_estop is False

    def test_set_fields(self):
        io = IoState(
            digital_in=(True, False),
            digital_out=(False, True),
            analog_in=(1.0, 2.0),
            analog_out=(3.0, 4.0),
            misc_error=(0, 1),
            pocket_prepped=5,
            tool_from_pocket=2,
            aux_estop=True,
        )
        assert io.digital_in == (True, False)
        assert io.digital_out == (False, True)
        assert io.analog_in == (1.0, 2.0)
        assert io.analog_out == (3.0, 4.0)
        assert io.misc_error == (0, 1)
        assert io.pocket_prepped == 5
        assert io.tool_from_pocket == 2
        assert io.aux_estop is True
