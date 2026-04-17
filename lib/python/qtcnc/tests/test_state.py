"""Tests for qtcnc.core.state — StateStore, diff, and apply."""

from __future__ import annotations

from dataclasses import fields

import pytest

from qtcnc.core.state import StateStore, apply, diff
from qtcnc.core.types import (
    AxisState,
    CoolantState,
    ExecState,
    InterpSettings,
    InterpState,
    IoState,
    JointState,
    MachineState,
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


class TestStateStoreDefaults:
    def test_default_is_safe(self):
        s = StateStore()
        assert s.connected is False
        assert s.task_state == TaskState.ESTOP
        assert s.machine.estop is True
        assert s.machine.powered is False
        assert s.position == Position()
        assert s.program == ProgramState()
        assert s.tool == Tool()
        assert s.spindles == (SpindleState(),)
        assert s.overrides == Overrides()
        assert s.active_gcodes == ()
        assert s.active_mcodes == ()


class TestDiffIdentity:
    def test_no_changes_on_identical_store(self):
        a = StateStore()
        b = StateStore()
        assert diff(a, b) == []

    def test_no_changes_on_same_instance(self):
        a = StateStore()
        assert diff(a, a) == []

    def test_diff_all_fields_on_fully_changed_store(self):
        old = StateStore()
        new = StateStore(
            connected=True,
            task_state=TaskState.ON,
            machine=MachineState(estop=False, powered=True, homed=(True, True, True)),
            position=Position(1, 2, 3),
            machine_position=Position(10, 20, 30),
            dtg=Position(-1, -2, -3),
            program=ProgramState(path="/tmp/f.ngc", total_lines=5, current_line=2),
            tool=Tool(id=1, pocket=1, diameter=6.0),
            tool_in_spindle=1,
            spindles=(SpindleState(speed=1000, direction=SpindleDir.FORWARD),),
            overrides=Overrides(feed=0.5, rapid=0.75, spindles=(1.2,)),
            feed_rate=100.0,
            rapid_rate=1000.0,
            active_gcodes=(20, 90),
            active_mcodes=(3,),
            task_info=TaskInfo(rcs_state=3, exec_state=ExecState.WAITING_FOR_IO),
            offsets=Offsets(g5x_index=2, rotation_xy=45.0),
            active_settings=InterpSettings(feed=100.0),
            joints=(JointState(output=1.0),),
            axes=(AxisState(velocity=5.0),),
            tool_table=(ToolEntry(id=1, diameter=6.0),),
            coolant=CoolantState(mist=True, flood=True),
            probe=ProbeState(tripped=True, value=1),
            io=IoState(digital_in=(True,), pocket_prepped=2, aux_estop=True),
            commanded_position=Position(x=5.0),
            heartbeat=123,
            taskbeat=456,
        )
        changes = diff(old, new)
        changed_names = {name for name, _ in changes}
        all_field_names = {f.name for f in fields(StateStore)}
        assert changed_names == all_field_names


class TestDiffSpecificFields:
    def test_single_field_change(self):
        old = StateStore()
        new = StateStore(feed_rate=50.0)
        changes = diff(old, new)
        assert changes == [("feed_rate", 50.0)]

    def test_nested_machine_change_emits_whole_field(self):
        old = StateStore()
        new = StateStore(machine=MachineState(estop=False, powered=True))
        changes = diff(old, new)
        assert len(changes) == 1
        name, value = changes[0]
        assert name == "machine"
        assert value.estop is False
        assert value.powered is True

    def test_position_change(self):
        old = StateStore()
        new = StateStore(position=Position(x=1.0, y=2.0, z=3.0))
        changes = diff(old, new)
        assert len(changes) == 1
        assert changes[0] == ("position", Position(x=1.0, y=2.0, z=3.0))

    def test_tuple_field_change(self):
        old = StateStore()
        new = StateStore(active_gcodes=(20, 90, 17))
        changes = diff(old, new)
        assert changes == [("active_gcodes", (20, 90, 17))]

    def test_spindles_tuple_replacement(self):
        old = StateStore()
        new = StateStore(spindles=(SpindleState(index=0, speed=1500),))
        changes = diff(old, new)
        assert len(changes) == 1
        name, value = changes[0]
        assert name == "spindles"
        assert value[0].speed == 1500

    def test_nested_task_info_change_emits_whole_field(self):
        old = StateStore()
        new = StateStore(task_info=TaskInfo(optional_stop=True))
        changes = diff(old, new)
        assert len(changes) == 1
        name, value = changes[0]
        assert name == "task_info"
        assert value.optional_stop is True

    def test_joints_tuple_single_field_mutation(self):
        old = StateStore(joints=(JointState(ferror_current=0.0),))
        new = StateStore(joints=(JointState(ferror_current=0.01),))
        changes = diff(old, new)
        assert len(changes) == 1
        assert changes[0][0] == "joints"
        assert changes[0][1][0].ferror_current == 0.01

    def test_tool_table_tuple_change(self):
        old = StateStore()
        new = StateStore(tool_table=(ToolEntry(id=1, diameter=6.0),))
        changes = diff(old, new)
        assert len(changes) == 1
        name, value = changes[0]
        assert name == "tool_table"
        assert len(value) == 1
        assert value[0].id == 1

    def test_heartbeat_only_diff(self):
        old = StateStore(heartbeat=10)
        new = StateStore(heartbeat=11)
        assert diff(old, new) == [("heartbeat", 11)]


class TestApply:
    def test_apply_empty_changes_is_same(self):
        s = StateStore()
        out = apply(s, [])
        assert out == s

    def test_apply_single_change(self):
        s = StateStore()
        out = apply(s, [("feed_rate", 100.0)])
        assert out.feed_rate == 100.0
        assert out != s

    def test_apply_multiple_changes(self):
        s = StateStore()
        out = apply(
            s,
            [
                ("feed_rate", 100.0),
                ("rapid_rate", 200.0),
                ("connected", True),
            ],
        )
        assert out.feed_rate == 100.0
        assert out.rapid_rate == 200.0
        assert out.connected is True

    def test_apply_nested_dataclass(self):
        s = StateStore()
        new_machine = MachineState(estop=False, powered=True)
        out = apply(s, [("machine", new_machine)])
        assert out.machine == new_machine

    def test_apply_unknown_field_raises(self):
        s = StateStore()
        with pytest.raises(TypeError):
            apply(s, [("no_such_field", 123)])


class TestDiffApplyRoundtrip:
    def test_diff_then_apply_reproduces_new(self):
        old = StateStore()
        new = StateStore(
            connected=True,
            feed_rate=50.0,
            position=Position(x=1, y=2, z=3),
            active_gcodes=(20, 90),
        )
        changes = diff(old, new)
        reconstructed = apply(old, changes)
        assert reconstructed == new

    def test_diff_empty_then_apply_is_noop(self):
        s = StateStore(feed_rate=5.0)
        changes = diff(s, s)
        out = apply(s, changes)
        assert out == s

    def test_cumulative_apply_chain(self):
        s0 = StateStore()
        s1 = apply(s0, [("feed_rate", 1.0)])
        s2 = apply(s1, [("rapid_rate", 2.0)])
        s3 = apply(s2, [("connected", True)])
        assert s3.feed_rate == 1.0
        assert s3.rapid_rate == 2.0
        assert s3.connected is True


class TestDiffOrderedByFieldDefinition:
    def test_change_order_matches_field_order(self):
        old = StateStore()
        new = StateStore(
            connected=True,
            feed_rate=1.0,
            rapid_rate=2.0,
            tool_in_spindle=3,
        )
        changes = diff(old, new)
        # Order must match the dataclass field declaration order so that
        # wire messages are deterministic and diffs are reproducible.
        field_names = [f.name for f in fields(StateStore)]
        changed_names = [name for name, _ in changes]
        expected_order = [n for n in field_names if n in changed_names]
        assert changed_names == expected_order
