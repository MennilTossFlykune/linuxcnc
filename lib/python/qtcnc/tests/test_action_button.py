"""Tests for the unified ActionButton widget."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from qtpy.QtWidgets import QApplication, QMainWindow

from qtcnc.core.command import Command
from qtcnc.core.status import Status
from qtcnc.core.types import (
    CoolantState,
    InterpState,
    JointState,
    MachineState,
    ProgramState,
    SpindleDir,
    SpindleState,
    TaskMode,
)
from qtcnc.signals import CommandVerb
from qtcnc.transport.mock import MockTransport
from qtcnc.widgets.common.action_button import ActionButton, ActionVerb
from qtcnc.widgets.common.dialog import QtcncDialog
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


def _make_world(*buttons: ActionButton, mutate: dict[str, Any] | None = None):
    t = _RecordingMock()
    if mutate:
        t.mutate_state(**mutate)
    window = QMainWindow()
    for b in buttons:
        b.setParent(window)
    status = Status(t)
    hub = HalPinHub(t)
    cmd = Command(t)
    window.qtcnc_status = status
    window.qtcnc_command = cmd
    window.qtcnc_hal = hub
    t.hello()
    status.bootstrap()
    for b in buttons:
        b.qtcnc_setup()
        b._test_world = window
    return t, status, cmd, window


def _ready_state(**overrides: Any) -> MachineState:
    base = dict(
        estop=False,
        powered=True,
        task_mode=TaskMode.MANUAL,
        interp_state=InterpState.IDLE,
        homed=(True, True, True),
        axis_count=3,
        axis_mask=0b111,
    )
    base.update(overrides)
    return MachineState(**base)


def _ready_mutate(**overrides: Any) -> dict[str, Any]:
    return {"machine": _ready_state(**overrides)}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestActionProperty:
    def test_default_is_estop(self):
        btn = ActionButton()
        assert btn.action == ActionVerb.estop
        assert btn.action.name == "estop"
        assert btn.isCheckable() is True

    def test_unknown_action_raises(self):
        btn = ActionButton()
        with pytest.raises(ValueError):
            btn._set_action("destroy_machine")

    def test_momentary_is_not_checkable(self):
        btn = ActionButton()
        btn._set_action("jog")
        assert btn.isCheckable() is False

    def test_trigger_is_not_checkable(self):
        btn = ActionButton()
        btn._set_action("auto_run")
        assert btn.isCheckable() is False

    def test_radio_is_checkable(self):
        btn = ActionButton()
        btn._set_action("mode")
        assert btn.isCheckable() is True


class TestRelevantPropertyNames:
    def test_estop_is_action_only(self):
        assert ActionButton.relevant_property_names(ActionVerb.estop) == {"action"}

    def test_jog_has_axis_joint_direction_velocity(self):
        assert ActionButton.relevant_property_names("jog") == {
            "action", "axis", "joint", "direction", "velocity",
        }

    def test_jog_increment_adds_distance(self):
        assert ActionButton.relevant_property_names("jog_increment") == {
            "action", "axis", "joint", "direction", "distance", "velocity",
        }

    def test_mdi_only_exposes_mdi_line(self):
        assert ActionButton.relevant_property_names("mdi") == {"action", "mdi_line"}

    def test_load_program_only_exposes_path(self):
        assert ActionButton.relevant_property_names("load_program") == {"action", "path"}

    def test_spindle_has_target_speed_index(self):
        assert ActionButton.relevant_property_names("spindle") == {
            "action", "target", "speed", "index",
        }

    def test_debug_exposes_mask(self):
        assert ActionButton.relevant_property_names("debug") == {"action", "mask"}

    def test_accepts_int_value(self):
        assert ActionButton.relevant_property_names(0) == {"action"}

    def test_every_action_returns_subset_of_aux_props(self):
        aux = ActionButton.all_auxiliary_property_names()
        from qtcnc.widgets.common.action_button import _ACTION_SPECS
        for name, spec in _ACTION_SPECS.items():
            leftover = set(spec.relevant_props) - aux - {"action"}
            assert not leftover, (
                f"action {name!r} names property {leftover!r} "
                f"that is not a Qt property on ActionButton"
            )

    def test_all_auxiliary_property_names_count(self):
        assert len(ActionButton.all_auxiliary_property_names()) == 14


# ---------------------------------------------------------------------------
# estop (toggle)
# ---------------------------------------------------------------------------


class TestEstopAction:
    def _mk(self, estop: bool = True) -> tuple[_RecordingMock, ActionButton]:
        btn = ActionButton()
        btn._set_action("estop")
        t, *_ = _make_world(btn, mutate={
            "machine": MachineState(estop=estop, powered=False,
                                    homed=(False, False, False),
                                    axis_count=3),
        })
        return t, btn

    def test_checked_reflects_estop_state(self):
        t, btn = self._mk(estop=True)
        assert btn.isChecked() is True
        assert btn.text() == "ESTOP RESET"

    def test_unchecked_when_not_estopped(self):
        btn = ActionButton()
        btn._set_action("estop")
        _make_world(btn, mutate=_ready_mutate())
        assert btn.isChecked() is False
        assert btn.text() == "ESTOP"

    def test_click_while_estopped_dispatches_reset(self):
        t, btn = self._mk(estop=True)
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert verbs[-1] == CommandVerb.STATE_ESTOP_RESET

    def test_click_while_cleared_dispatches_state_estop(self):
        btn = ActionButton()
        btn._set_action("estop")
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert verbs[-1] == CommandVerb.STATE_ESTOP

    def test_external_state_change_updates_label(self):
        t, btn = self._mk(estop=True)
        t.mutate_state(machine=_ready_state())
        QApplication.processEvents()
        assert btn.isChecked() is False
        assert btn.text() == "ESTOP"

    def test_estop_always_enabled(self):
        t, btn = self._mk(estop=True)
        assert btn.isEnabled() is True
        t.mutate_state(machine=_ready_state())
        QApplication.processEvents()
        assert btn.isEnabled() is True


# ---------------------------------------------------------------------------
# power (toggle)
# ---------------------------------------------------------------------------


class TestPowerAction:
    def _mk(self) -> tuple[_RecordingMock, ActionButton]:
        btn = ActionButton()
        btn._set_action("power")
        t, *_ = _make_world(btn)
        return t, btn

    def test_power_off_always_available_when_powered(self):
        btn = ActionButton()
        btn._set_action("power")
        t, *_ = _make_world(btn, mutate={
            "machine": MachineState(estop=True, powered=True,
                                    homed=(True, True, True),
                                    axis_count=3),
        })
        assert btn.isEnabled() is True
        assert btn.isChecked() is True
        assert btn.text() == "POWER OFF"

    def test_power_on_gated_on_estop_clear(self):
        t, btn = self._mk()
        assert btn.isEnabled() is False
        t.mutate_state(machine=MachineState(
            estop=False, powered=False, homed=(False, False, False),
            axis_count=3,
        ))
        QApplication.processEvents()
        assert btn.isEnabled() is True

    def test_click_while_off_dispatches_power_on(self):
        t, btn = self._mk()
        t.mutate_state(machine=MachineState(
            estop=False, powered=False, homed=(False, False, False),
            axis_count=3,
        ))
        QApplication.processEvents()
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert verbs[-1] == CommandVerb.STATE_ON


# ---------------------------------------------------------------------------
# mode (radio)
# ---------------------------------------------------------------------------


class TestModeAction:
    def _mk_trio(self) -> tuple[_RecordingMock, ActionButton, ActionButton, ActionButton]:
        manual = ActionButton()
        manual._set_action("mode")
        manual._set_target("manual")
        auto = ActionButton()
        auto._set_action("mode")
        auto._set_target("auto")
        mdi = ActionButton()
        mdi._set_action("mode")
        mdi._set_target("mdi")
        t, *_ = _make_world(manual, auto, mdi, mutate=_ready_mutate())
        return t, manual, auto, mdi

    def test_initial_manual_is_checked(self):
        _, manual, auto, mdi = self._mk_trio()
        assert manual.isChecked() is True
        assert auto.isChecked() is False
        assert mdi.isChecked() is False

    def test_click_auto_dispatches_set_mode(self):
        t, manual, auto, mdi = self._mk_trio()
        auto.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.SET_MODE]
        assert cmds[-1][1] == {"mode": TaskMode.AUTO}

    def test_mutating_mode_follows_state(self):
        t, manual, auto, mdi = self._mk_trio()
        t.mutate_state(machine=_ready_state(task_mode=TaskMode.AUTO))
        QApplication.processEvents()
        assert manual.isChecked() is False
        assert auto.isChecked() is True
        assert mdi.isChecked() is False

    def test_disabled_when_not_ready(self):
        t, manual, auto, mdi = self._mk_trio()
        t.mutate_state(machine=MachineState(
            estop=True, powered=False, homed=(False, False, False),
            axis_count=3,
        ))
        QApplication.processEvents()
        assert manual.isEnabled() is False
        assert auto.isEnabled() is False

    def test_missing_target_raises_on_dispatch(self):
        btn = ActionButton()
        btn._set_action("mode")
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        with pytest.raises(ValueError):
            btn._on_clicked()


# ---------------------------------------------------------------------------
# spindle (radio)
# ---------------------------------------------------------------------------


class TestSpindleAction:
    def _mk_trio(self) -> tuple[_RecordingMock, ActionButton, ActionButton, ActionButton]:
        cw = ActionButton()
        cw._set_action("spindle")
        cw._set_target("forward")
        cw._set_speed(1200.0)
        ccw = ActionButton()
        ccw._set_action("spindle")
        ccw._set_target("reverse")
        ccw._set_speed(800.0)
        stop = ActionButton()
        stop._set_action("spindle")
        stop._set_target("stop")
        t, *_ = _make_world(cw, ccw, stop, mutate=_ready_mutate())
        return t, cw, ccw, stop

    def test_initial_stop_checked(self):
        _, cw, ccw, stop = self._mk_trio()
        assert cw.isChecked() is False
        assert ccw.isChecked() is False
        assert stop.isChecked() is True

    def test_cw_click_dispatches_spindle_forward(self):
        t, cw, ccw, stop = self._mk_trio()
        cw.click()
        cmds = [(v, kw) for v, kw in t.commands
                if v == CommandVerb.SPINDLE_FORWARD]
        assert cmds[-1][1] == {"index": 0, "speed": 1200.0}

    def test_external_forward_checks_cw(self):
        t, cw, ccw, stop = self._mk_trio()
        t.mutate_state(spindles=(SpindleState(
            index=0, speed=1200.0, direction=SpindleDir.FORWARD, enabled=True,
        ),))
        QApplication.processEvents()
        assert cw.isChecked() is True
        assert stop.isChecked() is False

    def test_cw_ccw_disable_on_estop_stop_stays_enabled(self):
        t, cw, ccw, stop = self._mk_trio()
        t.mutate_state(machine=MachineState(
            estop=True, powered=False, homed=(False, False, False),
            axis_count=3,
        ))
        QApplication.processEvents()
        assert cw.isEnabled() is False
        assert ccw.isEnabled() is False
        assert stop.isEnabled() is True


# ---------------------------------------------------------------------------
# jog (momentary)
# ---------------------------------------------------------------------------


class TestJogAction:
    def _mk(self, axis: int = 0, direction: int = 1) -> tuple[_RecordingMock, ActionButton]:
        btn = ActionButton()
        btn._set_action("jog")
        btn._set_axis(axis)
        btn._set_direction(direction)
        btn._set_velocity(90.0)
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        return t, btn

    def test_press_dispatches_jog_continuous(self):
        t, btn = self._mk(axis=1, direction=1)
        btn.physical_pressed.emit()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.JOG_CONTINUOUS]
        assert cmds[-1][1] == {"axis": 1, "velocity": 90.0, "joint": -1}

    def test_release_dispatches_jog_stop(self):
        t, btn = self._mk()
        btn.physical_pressed.emit()
        btn.physical_released.emit()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.JOG_STOP]
        assert cmds[-1][1] == {"axis": 0, "joint": -1}

    def test_negative_direction_negates_velocity(self):
        t, btn = self._mk(axis=2, direction=-1)
        btn.physical_pressed.emit()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.JOG_CONTINUOUS]
        assert cmds[-1][1] == {"axis": 2, "velocity": -90.0, "joint": -1}

    def test_press_dispatches_with_explicit_joint(self):
        t, btn = self._mk(axis=1, direction=1)
        btn._set_joint(2)
        btn.physical_pressed.emit()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.JOG_CONTINUOUS]
        assert cmds[-1][1] == {"axis": 1, "velocity": 90.0, "joint": 2}

    def test_disabled_outside_manual(self):
        t, btn = self._mk()
        assert btn.isEnabled() is True
        t.mutate_state(machine=_ready_state(task_mode=TaskMode.AUTO))
        QApplication.processEvents()
        assert btn.isEnabled() is False

    def test_click_does_not_dispatch(self):
        t, btn = self._mk()
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert CommandVerb.JOG_CONTINUOUS not in verbs


# ---------------------------------------------------------------------------
# jog_increment (trigger)
# ---------------------------------------------------------------------------


class TestJogIncrementAction:
    def test_click_dispatches_jog_increment(self):
        btn = ActionButton()
        btn._set_action("jog_increment")
        btn._set_axis(1)
        btn._set_direction(1)
        btn._set_distance(0.5)
        btn._set_velocity(30.0)
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.JOG_INCREMENT]
        assert cmds[-1][1] == {"axis": 1, "distance": 0.5, "velocity": 30.0, "joint": -1}

    def test_negative_direction_negates_distance(self):
        btn = ActionButton()
        btn._set_action("jog_increment")
        btn._set_axis(0)
        btn._set_direction(-1)
        btn._set_distance(0.25)
        btn._set_velocity(60.0)
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.JOG_INCREMENT]
        assert cmds[-1][1] == {"axis": 0, "distance": -0.25, "velocity": 60.0, "joint": -1}


# ---------------------------------------------------------------------------
# home / unhome (triggers)
# ---------------------------------------------------------------------------


class TestHomeAction:
    def test_home_all_when_axis_negative(self):
        btn = ActionButton()
        btn._set_action("home")
        btn._set_axis(-1)
        t, *_ = _make_world(btn, mutate=_ready_mutate(homed=(False, False, False)))
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.HOME]
        assert cmds[-1][1] == {"joint": -1}

    def test_home_specific_axis(self):
        btn = ActionButton()
        btn._set_action("home")
        btn._set_axis(1)
        t, *_ = _make_world(btn, mutate=_ready_mutate(homed=(False, False, False)))
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.HOME]
        assert cmds[-1][1] == {"joint": 1}

    def test_unhome(self):
        btn = ActionButton()
        btn._set_action("unhome")
        btn._set_axis(2)
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.UNHOME]
        assert cmds[-1][1] == {"joint": 2}

    def test_home_disabled_in_auto(self):
        btn = ActionButton()
        btn._set_action("home")
        t, *_ = _make_world(btn, mutate=_ready_mutate(task_mode=TaskMode.AUTO))
        assert btn.isEnabled() is False

    def test_home_disabled_while_joint_is_homing(self):
        btn = ActionButton()
        btn._set_action("home")
        btn._set_axis(0)
        joints = (JointState(homing_state=1), JointState(), JointState())
        t, *_ = _make_world(btn, mutate=dict(
            machine=_ready_state(homed=(False, False, False)),
            joints=joints,
        ))
        assert btn.isEnabled() is False

    def test_home_enabled_when_other_joint_is_homing(self):
        btn = ActionButton()
        btn._set_action("home")
        btn._set_axis(0)
        joints = (JointState(), JointState(homing_state=1), JointState())
        t, *_ = _make_world(btn, mutate=dict(
            machine=_ready_state(homed=(False, False, False)),
            joints=joints,
        ))
        assert btn.isEnabled() is True

    def test_home_all_disabled_while_any_joint_is_homing(self):
        btn = ActionButton()
        btn._set_action("home")
        btn._set_axis(-1)
        joints = (JointState(), JointState(homing_state=1), JointState())
        t, *_ = _make_world(btn, mutate=dict(
            machine=_ready_state(homed=(False, False, False)),
            joints=joints,
        ))
        assert btn.isEnabled() is False

    def test_home_reenabled_after_homing_completes(self):
        btn = ActionButton()
        btn._set_action("home")
        btn._set_axis(0)
        joints_homing = (JointState(homing_state=1), JointState(), JointState())
        t, *_ = _make_world(btn, mutate=dict(
            machine=_ready_state(homed=(False, False, False)),
            joints=joints_homing,
        ))
        assert btn.isEnabled() is False
        joints_done = (JointState(homed=True), JointState(), JointState())
        t.mutate_state(joints=joints_done)
        QApplication.processEvents()
        assert btn.isEnabled() is True

    def test_unhome_disabled_while_joint_is_homing(self):
        btn = ActionButton()
        btn._set_action("unhome")
        btn._set_axis(1)
        joints = (JointState(), JointState(homing_state=1), JointState())
        t, *_ = _make_world(btn, mutate=dict(
            machine=_ready_state(),
            joints=joints,
        ))
        assert btn.isEnabled() is False


# ---------------------------------------------------------------------------
# program_* (triggers)
# ---------------------------------------------------------------------------


class TestProgramActions:
    def _mk(self, action: str) -> tuple[_RecordingMock, ActionButton]:
        btn = ActionButton()
        btn._set_action(action)
        t, *_ = _make_world(btn, mutate=_ready_mutate(task_mode=TaskMode.AUTO))
        return t, btn

    def test_auto_run_disabled_without_loaded_program(self):
        _, btn = self._mk("auto_run")
        assert btn.isEnabled() is False

    def test_auto_run_enabled_with_loaded_program(self):
        t, btn = self._mk("auto_run")
        t.load_program("/tmp/x.ngc")
        QApplication.processEvents()
        assert btn.isEnabled() is True

    def test_auto_run_dispatches(self):
        t, btn = self._mk("auto_run")
        t.load_program("/tmp/x.ngc")
        QApplication.processEvents()
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert CommandVerb.AUTO_RUN in verbs

    def test_abort_always_available_when_ready(self):
        _, btn = self._mk("abort")
        assert btn.isEnabled() is True

    def test_auto_pause_requires_running(self):
        t, btn = self._mk("auto_pause")
        assert btn.isEnabled() is False
        t.mutate_state(program=ProgramState(
            path="/tmp/x.ngc", is_running=True, is_paused=False,
        ))
        QApplication.processEvents()
        assert btn.isEnabled() is True


# ---------------------------------------------------------------------------
# mdi
# ---------------------------------------------------------------------------


class TestMdiAction:
    def test_click_dispatches_mdi_with_line(self):
        btn = ActionButton()
        btn._set_action("mdi")
        btn._set_mdi_line("G0 X5")
        t, *_ = _make_world(btn, mutate=_ready_mutate(task_mode=TaskMode.MDI))
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.MDI]
        assert cmds[-1][1] == {"command": "G0 X5"}

    def test_empty_line_does_not_dispatch(self):
        btn = ActionButton()
        btn._set_action("mdi")
        t, *_ = _make_world(btn, mutate=_ready_mutate(task_mode=TaskMode.MDI))
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert CommandVerb.MDI not in verbs

    def test_disabled_outside_mdi_mode(self):
        btn = ActionButton()
        btn._set_action("mdi")
        t, *_ = _make_world(btn, mutate=_ready_mutate(task_mode=TaskMode.MANUAL))
        assert btn.isEnabled() is False


# ---------------------------------------------------------------------------
# load_program
# ---------------------------------------------------------------------------


class TestLoadProgramAction:
    def test_explicit_path_dispatches_load(self):
        btn = ActionButton()
        btn._set_action("load_program")
        btn._set_path("/tmp/explicit.ngc")
        t, *_ = _make_world(btn)
        btn.click()
        assert btn.window().qtcnc_status.state.program.path == "/tmp/explicit.ngc"

    def test_disabled_while_program_running(self):
        btn = ActionButton()
        btn._set_action("load_program")
        t, *_ = _make_world(btn, mutate={
            **_ready_mutate(task_mode=TaskMode.AUTO),
            "program": ProgramState(
                path="/tmp/running.ngc", is_running=True, is_paused=False,
            ),
        })
        assert btn.isEnabled() is False


# ---------------------------------------------------------------------------
# mist / flood
# ---------------------------------------------------------------------------


class TestCoolantActions:
    def test_mist_on_dispatches(self):
        btn = ActionButton()
        btn._set_action("mist")
        btn._set_target("on")
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert CommandVerb.MIST_ON in verbs

    def test_mist_off_dispatches(self):
        btn = ActionButton()
        btn._set_action("mist")
        btn._set_target("off")
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert CommandVerb.MIST_OFF in verbs

    def test_flood_on_dispatches(self):
        btn = ActionButton()
        btn._set_action("flood")
        btn._set_target("on")
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert CommandVerb.FLOOD_ON in verbs

    def test_disabled_when_not_ready(self):
        btn = ActionButton()
        btn._set_action("mist")
        btn._set_target("on")
        t, *_ = _make_world(btn)
        assert btn.isEnabled() is False

    def test_missing_target_raises_on_dispatch(self):
        btn = ActionButton()
        btn._set_action("mist")
        t, *_ = _make_world(btn, mutate=_ready_mutate())
        with pytest.raises(ValueError):
            btn._on_clicked()


# ---------------------------------------------------------------------------
# mist_toggle / flood_toggle
# ---------------------------------------------------------------------------


class TestMistToggleAction:
    def _mk(self, mist: bool = False) -> tuple[_RecordingMock, ActionButton]:
        btn = ActionButton()
        btn._set_action("mist_toggle")
        t, *_ = _make_world(btn, mutate={
            **_ready_mutate(),
            "coolant": CoolantState(mist=mist),
        })
        return t, btn

    def test_checkable(self):
        _, btn = self._mk()
        assert btn.isCheckable() is True

    def test_unchecked_when_mist_off(self):
        _, btn = self._mk(mist=False)
        assert btn.isChecked() is False
        assert btn.text() == "MIST ON"

    def test_checked_when_mist_on(self):
        _, btn = self._mk(mist=True)
        assert btn.isChecked() is True
        assert btn.text() == "MIST OFF"

    def test_click_while_off_dispatches_mist_on(self):
        t, btn = self._mk(mist=False)
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert verbs[-1] == CommandVerb.MIST_ON

    def test_click_while_on_dispatches_mist_off(self):
        t, btn = self._mk(mist=True)
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert verbs[-1] == CommandVerb.MIST_OFF

    def test_external_state_change_updates(self):
        t, btn = self._mk(mist=False)
        assert btn.isChecked() is False
        t.mutate_state(coolant=CoolantState(mist=True))
        QApplication.processEvents()
        assert btn.isChecked() is True
        assert btn.text() == "MIST OFF"

    def test_disabled_when_not_ready(self):
        btn = ActionButton()
        btn._set_action("mist_toggle")
        t, *_ = _make_world(btn)
        assert btn.isEnabled() is False


class TestFloodToggleAction:
    def _mk(self, flood: bool = False) -> tuple[_RecordingMock, ActionButton]:
        btn = ActionButton()
        btn._set_action("flood_toggle")
        t, *_ = _make_world(btn, mutate={
            **_ready_mutate(),
            "coolant": CoolantState(flood=flood),
        })
        return t, btn

    def test_unchecked_when_flood_off(self):
        _, btn = self._mk(flood=False)
        assert btn.isChecked() is False
        assert btn.text() == "FLOOD ON"

    def test_checked_when_flood_on(self):
        _, btn = self._mk(flood=True)
        assert btn.isChecked() is True
        assert btn.text() == "FLOOD OFF"

    def test_click_while_off_dispatches_flood_on(self):
        t, btn = self._mk(flood=False)
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert verbs[-1] == CommandVerb.FLOOD_ON

    def test_click_while_on_dispatches_flood_off(self):
        t, btn = self._mk(flood=True)
        btn.click()
        verbs = [v for v, _ in t.commands]
        assert verbs[-1] == CommandVerb.FLOOD_OFF

    def test_external_state_change_updates(self):
        t, btn = self._mk(flood=False)
        t.mutate_state(coolant=CoolantState(flood=True))
        QApplication.processEvents()
        assert btn.isChecked() is True
        assert btn.text() == "FLOOD OFF"


# ---------------------------------------------------------------------------
# home_toggle
# ---------------------------------------------------------------------------


class TestHomeToggleAction:
    def _mk(self, axis: int = 0, homed: tuple[bool, ...] = (False, False, False),
            joint: int = -1, **machine_kw: Any) -> tuple[_RecordingMock, ActionButton]:
        btn = ActionButton()
        btn._set_action("home_toggle")
        btn._set_axis(axis)
        if joint >= 0:
            btn._set_joint(joint)
        t, *_ = _make_world(btn, mutate=_ready_mutate(homed=homed, **machine_kw))
        return t, btn

    def test_not_checked(self):
        _, btn = self._mk()
        assert btn.isChecked() is False

    def test_label_home_when_unhomed(self):
        _, btn = self._mk(axis=0, homed=(False, True, True))
        assert btn.text() == "HOME X"

    def test_label_unhome_when_homed(self):
        _, btn = self._mk(axis=0, homed=(True, True, True))
        assert btn.isChecked() is False
        assert btn.text() == "UNHOME X"

    def test_label_uses_axis_letter(self):
        _, btn = self._mk(axis=2, homed=(True, True, False))
        assert btn.text() == "HOME Z"

    def test_click_when_unhomed_dispatches_home(self):
        t, btn = self._mk(axis=1, homed=(True, False, True))
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.HOME]
        assert cmds[-1][1] == {"joint": 1}

    def test_click_when_homed_dispatches_unhome(self):
        t, btn = self._mk(axis=1, homed=(True, True, True))
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.UNHOME]
        assert cmds[-1][1] == {"joint": 1}

    def test_explicit_joint_overrides_axis(self):
        t, btn = self._mk(axis=0, homed=(True, False, True), joint=2)
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.UNHOME]
        assert cmds[-1][1] == {"joint": 2}

    def test_gantry_homes_both_joints_for_axis(self):
        t, btn = self._mk(
            axis=1,
            homed=(True, False, False, True),
            coordinates=("X", "Y", "Y", "Z"),
            axis_count=4, axis_mask=0b111,
        )
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.HOME]
        joints = [kw["joint"] for v, kw in cmds]
        assert joints == [1, 2]

    def test_gantry_unhomes_both_joints_for_axis(self):
        t, btn = self._mk(
            axis=1,
            homed=(True, True, True, True),
            coordinates=("X", "Y", "Y", "Z"),
            axis_count=4, axis_mask=0b111,
        )
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.UNHOME]
        joints = [kw["joint"] for v, kw in cmds]
        assert joints == [1, 2]

    def test_home_all_when_axis_negative(self):
        t, btn = self._mk(axis=-1, homed=(False, False, False))
        assert btn.text() == "HOME ALL"
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.HOME]
        assert cmds[-1][1] == {"joint": -1}

    def test_unhome_all_when_all_homed(self):
        t, btn = self._mk(axis=-1, homed=(True, True, True))
        assert btn.text() == "UNHOME ALL"
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.UNHOME]
        joints = [kw["joint"] for v, kw in t.commands if v == CommandVerb.UNHOME]
        assert joints == [0, 1, 2]

    def test_external_homed_change_updates_label(self):
        t, btn = self._mk(axis=0, homed=(False, False, False))
        assert btn.text() == "HOME X"
        t.mutate_state(machine=_ready_state(homed=(True, False, False)))
        QApplication.processEvents()
        assert btn.isChecked() is False
        assert btn.text() == "UNHOME X"

    def test_disabled_in_auto_mode(self):
        btn = ActionButton()
        btn._set_action("home_toggle")
        t, *_ = _make_world(btn, mutate=_ready_mutate(task_mode=TaskMode.AUTO))
        assert btn.isEnabled() is False

    def test_disabled_when_estopped(self):
        btn = ActionButton()
        btn._set_action("home_toggle")
        t, *_ = _make_world(btn)
        assert btn.isEnabled() is False

    def test_confirm_accepted_dispatches_unhome(self, monkeypatch):
        t, btn = self._mk(axis=1, homed=(True, True, True))
        btn._set_confirm(True)
        monkeypatch.setattr(
            QtcncDialog, "exec",
            lambda self: QtcncDialog.DialogCode.Accepted,
        )
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.UNHOME]
        assert cmds[-1][1] == {"joint": 1}

    def test_confirm_rejected_blocks_unhome(self, monkeypatch):
        t, btn = self._mk(axis=1, homed=(True, True, True))
        btn._set_confirm(True)
        monkeypatch.setattr(
            QtcncDialog, "exec",
            lambda self: QtcncDialog.DialogCode.Rejected,
        )
        btn.click()
        cmds = [v for v, _ in t.commands if v == CommandVerb.UNHOME]
        assert cmds == []

    def test_confirm_false_skips_dialog(self):
        t, btn = self._mk(axis=1, homed=(True, True, True))
        btn._set_confirm(False)
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.UNHOME]
        assert cmds[-1][1] == {"joint": 1}

    def test_confirm_not_shown_when_homing(self, monkeypatch):
        dialog_shown = []
        t, btn = self._mk(axis=1, homed=(False, False, False))
        btn._set_confirm(True)
        monkeypatch.setattr(
            QtcncDialog, "exec",
            lambda self: dialog_shown.append(True) or QtcncDialog.DialogCode.Accepted,
        )
        btn.click()
        cmds = [(v, kw) for v, kw in t.commands if v == CommandVerb.HOME]
        assert cmds[-1][1] == {"joint": 1}
        assert dialog_shown == []

    def test_confirm_dialog_says_axis_not_joint(self, monkeypatch):
        messages = []
        orig_init = QtcncDialog.__init__
        def capture_init(self, *a, **kw):
            messages.append(kw.get("message", ""))
            orig_init(self, *a, **kw)
        monkeypatch.setattr(QtcncDialog, "__init__", capture_init)
        monkeypatch.setattr(
            QtcncDialog, "exec",
            lambda self: QtcncDialog.DialogCode.Accepted,
        )
        t, btn = self._mk(axis=1, homed=(True, True, True))
        btn._set_confirm(True)
        btn.click()
        assert messages and "Y" in messages[-1]
        assert "joint" not in messages[-1].lower()

    def test_label_homing_single_axis(self):
        btn = ActionButton()
        btn._set_action("home_toggle")
        btn._set_axis(0)
        joints = (JointState(homing_state=1), JointState(), JointState())
        t, *_ = _make_world(btn, mutate=dict(
            machine=_ready_state(homed=(False, False, False)),
            joints=joints,
        ))
        assert btn.text() == "HOMING X..."

    def test_label_homing_all(self):
        btn = ActionButton()
        btn._set_action("home_toggle")
        btn._set_axis(-1)
        joints = (JointState(homing_state=1), JointState(homing_state=1), JointState())
        t, *_ = _make_world(btn, mutate=dict(
            machine=_ready_state(homed=(False, False, False)),
            joints=joints,
        ))
        assert btn.text() == "HOMING..."

    def test_disabled_while_homing(self):
        btn = ActionButton()
        btn._set_action("home_toggle")
        btn._set_axis(0)
        joints = (JointState(homing_state=1), JointState(), JointState())
        t, *_ = _make_world(btn, mutate=dict(
            machine=_ready_state(homed=(False, False, False)),
            joints=joints,
        ))
        assert btn.isEnabled() is False

    def test_reenabled_after_homing_completes(self):
        btn = ActionButton()
        btn._set_action("home_toggle")
        btn._set_axis(0)
        joints_homing = (JointState(homing_state=1), JointState(), JointState())
        t, *_ = _make_world(btn, mutate=dict(
            machine=_ready_state(homed=(False, False, False)),
            joints=joints_homing,
        ))
        assert btn.isEnabled() is False
        assert btn.text() == "HOMING X..."
        joints_done = (JointState(homed=True), JointState(), JointState())
        t.mutate_state(
            machine=_ready_state(homed=(True, False, False)),
            joints=joints_done,
        )
        QApplication.processEvents()
        assert btn.isEnabled() is True
        assert btn.text() == "UNHOME X"
