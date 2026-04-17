"""Tests for state predicates on MachineState, ProgramState, and StateStore.

Each predicate is verified:
- Positive path — the predicate is True in an expected state.
- Negative paths — the predicate flips to False when any guarding field
  is wrong.

No Qt, no linuxcnc. Pure dataclass property reads.
"""

from __future__ import annotations

from dataclasses import replace

from qtcnc.core.state import StateStore
from qtcnc.core.types import (
    InterpState,
    JointState,
    MachineState,
    MotionMode,
    ProgramState,
    TaskMode,
    ToolEntry,
)


def _ok_machine(**kwargs) -> MachineState:
    """MachineState in the default 'ready, MANUAL, idle' happy path."""
    base = dict(
        estop=False,
        powered=True,
        task_mode=TaskMode.MANUAL,
        interp_state=InterpState.IDLE,
        homed=(True, True, True),
        kinematics_identity=True,
        motion_mode=MotionMode.TELEOP,
    )
    base.update(kwargs)
    return MachineState(**base)


class TestMachineStatePrimitives:
    def test_is_ready_requires_both_flags(self):
        assert not MachineState(estop=True, powered=False).is_ready
        assert not MachineState(estop=True, powered=True).is_ready
        assert not MachineState(estop=False, powered=False).is_ready
        assert MachineState(estop=False, powered=True).is_ready

    def test_is_all_homed_empty_tuple_is_false(self):
        assert not MachineState(homed=()).is_all_homed

    def test_is_all_homed_partial_is_false(self):
        assert not MachineState(homed=(True, False, True)).is_all_homed

    def test_is_all_homed_all_true(self):
        assert MachineState(homed=(True, True, True)).is_all_homed
        assert MachineState(homed=(True,)).is_all_homed

    def test_is_idle(self):
        assert MachineState(interp_state=InterpState.IDLE).is_idle
        assert not MachineState(interp_state=InterpState.READING).is_idle
        assert not MachineState(interp_state=InterpState.PAUSED).is_idle
        assert not MachineState(interp_state=InterpState.WAITING).is_idle

    def test_task_mode_primitives(self):
        m = MachineState(task_mode=TaskMode.MANUAL)
        assert m.is_manual and not m.is_auto and not m.is_mdi
        m = replace(m, task_mode=TaskMode.AUTO)
        assert not m.is_manual and m.is_auto and not m.is_mdi
        m = replace(m, task_mode=TaskMode.MDI)
        assert not m.is_manual and not m.is_auto and m.is_mdi


class TestMachineStateComposites:
    def test_can_jog_happy_path(self):
        assert _ok_machine().can_jog

    def test_can_jog_requires_ready(self):
        assert not _ok_machine(estop=True).can_jog
        assert not _ok_machine(powered=False).can_jog

    def test_can_jog_requires_manual(self):
        assert not _ok_machine(task_mode=TaskMode.AUTO).can_jog
        assert not _ok_machine(task_mode=TaskMode.MDI).can_jog

    def test_can_jog_requires_idle(self):
        assert not _ok_machine(interp_state=InterpState.READING).can_jog
        assert not _ok_machine(interp_state=InterpState.PAUSED).can_jog

    def test_can_home_ignores_interp_state(self):
        m = _ok_machine(interp_state=InterpState.READING)
        assert m.can_home

    def test_can_home_requires_manual_ready(self):
        assert _ok_machine().can_home
        assert not _ok_machine(estop=True).can_home
        assert not _ok_machine(powered=False).can_home
        assert not _ok_machine(task_mode=TaskMode.AUTO).can_home

    def test_can_unhome_matches_can_home(self):
        assert _ok_machine().can_unhome
        assert not _ok_machine(task_mode=TaskMode.AUTO).can_unhome

    def test_can_spindle_gates_only_on_ready(self):
        assert _ok_machine().can_spindle
        assert _ok_machine(task_mode=TaskMode.AUTO).can_spindle
        assert _ok_machine(task_mode=TaskMode.MDI).can_spindle
        assert not _ok_machine(estop=True).can_spindle
        assert not _ok_machine(powered=False).can_spindle

    def test_can_use_teleop_jog_needs_identity_and_all_homed(self):
        assert _ok_machine().can_use_teleop_jog
        assert not _ok_machine(kinematics_identity=False).can_use_teleop_jog
        assert not _ok_machine(homed=(True, False, True)).can_use_teleop_jog
        assert not _ok_machine(homed=()).can_use_teleop_jog


class TestProgramStatePredicates:
    def test_has_program(self):
        assert ProgramState(path="/p.ngc").has_program
        assert not ProgramState(path="").has_program

    def test_is_executing_covers_running_and_paused(self):
        assert ProgramState(is_running=True).is_executing
        assert ProgramState(is_paused=True).is_executing
        assert ProgramState(is_running=True, is_paused=True).is_executing
        assert not ProgramState().is_executing

    def test_is_editable_is_inverse_of_is_executing(self):
        assert ProgramState().is_editable
        assert ProgramState(path="/p.ngc").is_editable
        assert not ProgramState(is_running=True).is_editable
        assert not ProgramState(is_paused=True).is_editable

    def test_is_runnable_requires_path_and_not_executing(self):
        assert not ProgramState().is_runnable
        assert ProgramState(path="/p.ngc").is_runnable
        assert not ProgramState(path="/p.ngc", is_running=True).is_runnable
        assert not ProgramState(path="/p.ngc", is_paused=True).is_runnable


class TestStateStoreComposites:
    def _store(self, *, machine=None, program=None) -> StateStore:
        return StateStore(
            machine=machine if machine is not None else _ok_machine(task_mode=TaskMode.AUTO),
            program=program if program is not None else ProgramState(path="/p.ngc"),
        )

    def test_can_run_program_happy_path(self):
        assert self._store().can_run_program

    def test_can_run_program_requires_loaded_file(self):
        s = self._store(program=ProgramState(path=""))
        assert not s.can_run_program

    def test_can_run_program_refused_while_running(self):
        s = self._store(program=ProgramState(path="/p.ngc", is_running=True))
        assert not s.can_run_program

    def test_can_run_program_requires_auto_mode(self):
        s = self._store(machine=_ok_machine(task_mode=TaskMode.MANUAL))
        assert not s.can_run_program

    def test_can_run_program_requires_ready_and_idle(self):
        s = self._store(machine=_ok_machine(task_mode=TaskMode.AUTO, estop=True))
        assert not s.can_run_program
        s = self._store(
            machine=_ok_machine(
                task_mode=TaskMode.AUTO, interp_state=InterpState.READING
            ),
        )
        assert not s.can_run_program

    def test_can_run_program_refused_when_tools_missing(self):
        s = self._store(
            program=ProgramState(path="/p.ngc", requested_tools=frozenset({1, 5})),
        )
        s = replace(s, tool_table=(ToolEntry(id=1),))
        assert not s.can_run_program

    def test_can_run_program_passes_when_all_tools_present(self):
        s = self._store(
            program=ProgramState(path="/p.ngc", requested_tools=frozenset({1, 5})),
        )
        s = replace(s, tool_table=(ToolEntry(id=1), ToolEntry(id=5)))
        assert s.can_run_program

    def test_can_run_program_no_requested_tools_is_ok(self):
        s = self._store(program=ProgramState(path="/p.ngc"))
        assert s.can_run_program

    def test_can_run_program_requires_all_homed(self):
        s = self._store(machine=_ok_machine(task_mode=TaskMode.AUTO, homed=(True, False, True)))
        assert not s.can_run_program

    def test_missing_tools_returns_absent_ids(self):
        s = self._store(
            program=ProgramState(path="/p.ngc", requested_tools=frozenset({1, 5, 99})),
        )
        s = replace(s, tool_table=(ToolEntry(id=1), ToolEntry(id=5)))
        assert s.missing_tools == frozenset({99})

    def test_missing_tools_empty_when_all_present(self):
        s = self._store(
            program=ProgramState(path="/p.ngc", requested_tools=frozenset({1})),
        )
        s = replace(s, tool_table=(ToolEntry(id=1),))
        assert s.missing_tools == frozenset()

    def test_missing_tools_empty_when_no_requested_tools(self):
        s = self._store()
        assert s.missing_tools == frozenset()

    def test_can_resume_program_requires_paused(self):
        s = self._store(program=ProgramState(path="/p.ngc", is_paused=True))
        assert s.can_resume_program
        s = self._store(program=ProgramState(path="/p.ngc"))
        assert not s.can_resume_program

    def test_can_resume_program_requires_auto(self):
        s = self._store(
            machine=_ok_machine(task_mode=TaskMode.MANUAL),
            program=ProgramState(path="/p.ngc", is_paused=True),
        )
        assert not s.can_resume_program

    def test_can_pause_program(self):
        s = self._store(program=ProgramState(path="/p.ngc", is_running=True))
        assert s.can_pause_program
        s = self._store(
            program=ProgramState(path="/p.ngc", is_running=True, is_paused=True),
        )
        assert not s.can_pause_program
        s = self._store(program=ProgramState(path="/p.ngc"))
        assert not s.can_pause_program

    def test_can_abort_program_gates_on_ready(self):
        s = self._store()
        assert s.can_abort_program
        s = self._store(machine=_ok_machine(task_mode=TaskMode.AUTO, estop=True))
        assert not s.can_abort_program

    def test_can_step_program(self):
        s = self._store()
        assert s.can_step_program
        s = self._store(program=ProgramState(path="", is_paused=True))
        assert s.can_step_program
        s = self._store(program=ProgramState(path=""))
        assert not s.can_step_program
        s = self._store(machine=_ok_machine(task_mode=TaskMode.MANUAL))
        assert not s.can_step_program

    def test_can_mdi(self):
        s = self._store(machine=_ok_machine(task_mode=TaskMode.MDI))
        assert s.can_mdi
        s = self._store(machine=_ok_machine(task_mode=TaskMode.MANUAL))
        assert not s.can_mdi
        s = self._store(
            machine=_ok_machine(
                task_mode=TaskMode.MDI, interp_state=InterpState.READING
            ),
        )
        assert not s.can_mdi

    def test_can_load_program_allows_drives_off(self):
        s = self._store(
            machine=_ok_machine(task_mode=TaskMode.AUTO, estop=True, powered=False),
        )
        assert s.can_load_program

    def test_can_load_program_refused_while_executing(self):
        s = self._store(program=ProgramState(path="/p.ngc", is_running=True))
        assert not s.can_load_program
        s = self._store(program=ProgramState(path="/p.ngc", is_paused=True))
        assert not s.can_load_program

    def test_can_edit_program_is_alias_for_is_editable(self):
        s = self._store()
        assert s.can_edit_program == s.program.is_editable
        s = self._store(program=ProgramState(path="/p.ngc", is_running=True))
        assert s.can_edit_program == s.program.is_editable


class TestDefaultStateStoreLocksEverything:
    """A default StateStore() has estop=True and powered=False, so every
    predicate that gates on readiness must return False. Widgets reading
    predicates before the first state diff see a locked UI by default.
    """

    def test_default_machine_predicates_are_false(self):
        m = MachineState()
        assert not m.is_ready
        assert not m.can_jog
        assert not m.can_home
        assert not m.can_spindle
        assert not m.can_use_teleop_jog

    def test_default_store_composites_are_false(self):
        s = StateStore()
        assert not s.can_run_program
        assert not s.can_resume_program
        assert not s.can_pause_program
        assert not s.can_abort_program
        assert not s.can_step_program
        assert not s.can_mdi
        # load is permitted: no program is executing by default
        assert s.can_load_program
        assert s.can_edit_program


class TestJointStatePredicates:
    def test_is_homing_true(self):
        assert JointState(homing_state=1).is_homing

    def test_is_homing_false(self):
        assert not JointState(homing_state=0).is_homing
        assert not JointState().is_homing


class TestHomingPredicates:
    def test_is_any_homing_true(self):
        s = StateStore(joints=(JointState(), JointState(homing_state=1), JointState()))
        assert s.is_any_homing

    def test_is_any_homing_false(self):
        s = StateStore(joints=(JointState(), JointState(), JointState()))
        assert not s.is_any_homing

    def test_is_any_homing_empty_joints(self):
        s = StateStore(joints=())
        assert not s.is_any_homing
