"""Smoke tests for the Qt Designer plugin module.

These tests do NOT launch Designer itself. They prove that
``qtcnc.designer.qtcnc_widget_plugins`` imports cleanly, every plugin
class instantiates, ``createWidget(parent)`` returns an instance of the
right widget type, and each plugin's ``domXml`` pre-declares every
Designer-visible Qt property with ``stdset="0"`` so the property editor
shows them after drop.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from enum import Enum
from typing import Any

import pytest

from qtpy.QtWidgets import QApplication


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class _FakeSignal:
    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in list(self._slots):
            slot(*args)


class _FakePropertyEditor:
    def __init__(self) -> None:
        self._object: Any = None
        self.set_object_calls: list[Any] = []
        self.propertyChanged = _FakeSignal()

    def object(self) -> Any:
        return self._object

    def setObject(self, obj: Any) -> None:
        self._object = obj
        self.set_object_calls.append(obj)


class _FakeSheet:
    def __init__(self, names: tuple[str, ...]) -> None:
        self._names = tuple(names)
        self.visibility: dict[str, bool] = {}

    def indexOf(self, name: str) -> int:
        try:
            return self._names.index(name)
        except ValueError:
            return -1

    def propertyName(self, index: int) -> str:
        return self._names[index]

    def setVisible(self, index: int, visible: bool) -> None:
        self.visibility[self._names[index]] = bool(visible)


class _FakeExtensionManager:
    def __init__(self, sheet: Any | None = None) -> None:
        self.registered: list[tuple[object, str]] = []
        self._sheet = sheet

    def registerExtensions(self, factory: object, iid: str) -> None:
        self.registered.append((factory, iid))

    def extension(self, _obj: Any, _iid: str) -> Any:
        return self._sheet


class _FakeFormWindowManager:
    def __init__(self, form_windows: list[Any] | None = None) -> None:
        self.formWindowAdded = _FakeSignal()
        self._form_windows: list[Any] = list(form_windows or [])

    def formWindowCount(self) -> int:
        return len(self._form_windows)

    def formWindow(self, i: int) -> Any:
        return self._form_windows[i]

    def add_form_window(self, form: Any) -> None:
        self._form_windows.append(form)


class _FakeFormEditor:
    def __init__(
        self,
        *,
        property_editor: Any = None,
        sheet: Any = None,
        form_window_manager: Any = None,
    ) -> None:
        self.extension_manager = _FakeExtensionManager(sheet=sheet)
        self._property_editor = property_editor
        self._form_window_manager = form_window_manager

    def extensionManager(self) -> _FakeExtensionManager:
        return self.extension_manager

    def propertyEditor(self) -> Any:
        return self._property_editor

    def formWindowManager(self) -> Any:
        return self._form_window_manager


class _FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def setProperty(self, name: str, value: Any) -> None:
        self.calls.append((name, value))


class _FakeContainer:
    def __init__(self, children: list[Any]) -> None:
        self._children = list(children)

    def findChildren(self, cls: type) -> list[Any]:
        return [c for c in self._children if isinstance(c, cls)]


class _FakeFormWindow:
    def __init__(self, core: Any, main_container: Any = None) -> None:
        self._core = core
        self._cursor = _FakeCursor()
        self._main_container = main_container
        self.widgetManaged = _FakeSignal()

    def core(self) -> Any:
        return self._core

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def mainContainer(self) -> Any:
        return self._main_container


@pytest.fixture(autouse=True)
def designer_env():
    prev = os.environ.get("DESIGNER")
    os.environ["DESIGNER"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("DESIGNER", None)
        else:
            os.environ["DESIGNER"] = prev


def _expected_pairs():
    from qtcnc.designer import qtcnc_widget_plugin as plugins
    from qtcnc.widgets.common.action_button import ActionButton
    from qtcnc.widgets.common.dro_grid import DroGrid
    from qtcnc.widgets.common.gcode_preview import GcodePreview
    from qtcnc.widgets.common.gcode_view import GcodeView
    from qtcnc.widgets.common.mdi_entry import MdiEntry
    from qtcnc.widgets.common.override_slider import OverrideSlider
    from qtcnc.widgets.common.state_label import StateLabel

    return [
        (plugins.ActionButtonPlugin, ActionButton),
        (plugins.StateLabelPlugin, StateLabel),
        (plugins.DroGridPlugin, DroGrid),
        (plugins.OverrideSliderPlugin, OverrideSlider),
        (plugins.GcodeViewPlugin, GcodeView),
        (plugins.GcodePreviewPlugin, GcodePreview),
        (plugins.MdiEntryPlugin, MdiEntry),
    ]


_EXPECTED_CUSTOM_PROPS: dict[str, list[tuple[str, str, object]]] = {
    "ActionButtonPlugin": [
        ("target", "string", ""),
        ("axis", "number", 0),
        ("direction", "number", 1),
        ("velocity", "double", 60.0),
        ("distance", "double", 1.0),
        ("speed", "double", 1000.0),
        ("index", "number", 0),
        ("mdi_line", "string", ""),
        ("path", "string", ""),
        ("value", "double", 0.0),
        ("message", "string", ""),
        ("mask", "number", 0),
    ],
    "StateLabelPlugin": [
        ("state", "string", "task_state"),
        ("axis", "number", 0),
        ("index", "number", 0),
        ("format_string", "string", ""),
    ],
    "DroGridPlugin": [
        ("coord_system", "string", "work"),
        ("format_string", "string", ""),
    ],
    "OverrideSliderPlugin": [
        ("kind", "string", "feed"),
        ("index", "number", 0),
        ("max_value", "double", 1.5),
    ],
    "GcodeViewPlugin": [],
    "GcodePreviewPlugin": [],
    "MdiEntryPlugin": [
        ("submit_on_enter", "bool", True),
        ("clear_on_submit", "bool", True),
    ],
}


class TestPluginRegistry:
    def test_module_imports_cleanly(self):
        from qtcnc.designer import qtcnc_widget_plugin  # noqa: F401

    def test_every_widget_has_a_plugin(self):
        pairs = _expected_pairs()
        assert len(pairs) == 7
        names = {plugin.__name__ for plugin, _ in pairs}
        assert "ActionButtonPlugin" in names
        assert "MdiEntryPlugin" in names

    def test_each_plugin_instantiates(self):
        for plugin_cls, _ in _expected_pairs():
            instance = plugin_cls()
            assert instance.name() == plugin_cls.__name__.removesuffix("Plugin")
            assert instance.group() == "qtcnc"
            assert "<widget" in instance.domXml()
            assert instance.includeFile().startswith("qtcnc.widgets.common.")

    def test_create_widget_returns_correct_type(self):
        for plugin_cls, widget_cls in _expected_pairs():
            instance = plugin_cls()
            widget = instance.createWidget(None)
            assert isinstance(widget, widget_cls)
            widget.setObjectName(widget_cls.__name__.lower())
            widget.deleteLater()

    def test_initialize_is_idempotent(self):
        fake_editor = _FakeFormEditor(property_editor=_FakePropertyEditor())
        plugin_cls = _expected_pairs()[0][0]
        instance = plugin_cls()
        assert instance.isInitialized() is False
        instance.initialize(fake_editor)
        assert instance.isInitialized() is True
        instance.initialize(fake_editor)
        assert instance.isInitialized() is True

    def test_custom_properties_match_expected(self):
        for plugin_cls, _ in _expected_pairs():
            expected = _EXPECTED_CUSTOM_PROPS[plugin_cls.__name__]
            actual = plugin_cls().custom_properties()
            assert actual == expected, (
                f"{plugin_cls.__name__}.custom_properties() drifted from the "
                f"expected list — update _EXPECTED_CUSTOM_PROPS or the plugin"
            )

    def test_dom_xml_is_well_formed(self):
        for plugin_cls, _ in _expected_pairs():
            xml = plugin_cls().domXml()
            root = ET.fromstring(xml)
            assert root.tag == "widget"
            assert root.attrib["class"] == plugin_cls.__name__.removesuffix("Plugin")

    def test_dom_xml_pre_declares_every_custom_property(self):
        for plugin_cls, _ in _expected_pairs():
            expected = _EXPECTED_CUSTOM_PROPS[plugin_cls.__name__]
            root = ET.fromstring(plugin_cls().domXml())
            custom = [
                p for p in root.findall("property")
                if p.attrib.get("stdset") == "0"
            ]
            assert len(custom) == len(expected), (
                f"{plugin_cls.__name__}: expected {len(expected)} stdset=\"0\" "
                f"properties in domXml, found {len(custom)}"
            )
            for (prop_name, type_name, default), node in zip(expected, custom):
                assert node.attrib["name"] == prop_name
                child = list(node)
                assert len(child) == 1
                assert child[0].tag == type_name
                text = child[0].text or ""
                if type_name == "string":
                    assert text == str(default)
                elif type_name == "bool":
                    assert text == ("true" if default else "false")
                elif type_name == "number":
                    assert int(text) == int(default)
                elif type_name == "double":
                    assert float(text) == pytest.approx(float(default))
                else:
                    pytest.fail(f"unknown type tag {type_name!r}")

    def test_created_widget_accepts_every_custom_property(self):
        for plugin_cls, _ in _expected_pairs():
            expected = _EXPECTED_CUSTOM_PROPS[plugin_cls.__name__]
            if not expected:
                continue
            widget = plugin_cls().createWidget(None)
            try:
                for prop_name, _type_name, default in expected:
                    ok = widget.setProperty(prop_name, default)
                    assert ok, (
                        f"{plugin_cls.__name__}: widget refused "
                        f"setProperty({prop_name!r}, {default!r}) — "
                        f"Property declaration missing or mistyped"
                    )
                    got = getattr(widget, prop_name)
                    if isinstance(got, Enum):
                        assert got.name == default
                    elif isinstance(default, float):
                        assert float(got) == pytest.approx(default)
                    else:
                        assert got == default
            finally:
                widget.deleteLater()


class TestActionButtonEditorDialog:
    """Dialog seeds from widget, hides non-relevant rows, writes back on OK."""

    def _mk(self, action: str = "estop", **props: Any):
        from qtcnc.designer.action_button_editor import ActionButtonEditorDialog
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        btn._set_action(action)
        for name, value in props.items():
            setter = getattr(btn, f"_set_{name}")
            setter(value)
        return btn, ActionButtonEditorDialog(btn)

    def _visible_aux(self, dialog) -> set[str]:
        return {
            name
            for name, (label, editor) in dialog._param_rows.items()
            if label.isVisibleTo(dialog) and editor.isVisibleTo(dialog)
        }

    def test_seeds_action_from_widget(self):
        _btn, d = self._mk("jog")
        assert d._selected_action_name() == "jog"

    def test_seeds_int_and_float_editors(self):
        _btn, d = self._mk("jog", axis=3, direction=-1, velocity=25.5)
        assert d._param_rows["axis"][1].value() == 3
        assert d._param_rows["direction"][1].value() == -1
        assert d._param_rows["velocity"][1].value() == pytest.approx(25.5)

    def test_seeds_string_editors(self):
        _btn, d = self._mk("mdi", mdi_line="G0 X10")
        assert d._param_rows["mdi_line"][1].text() == "G0 X10"

    def test_estop_hides_every_parameter_row(self):
        _btn, d = self._mk("estop")
        assert self._visible_aux(d) == set()

    def test_jog_shows_axis_joint_direction_velocity(self):
        _btn, d = self._mk("jog")
        assert self._visible_aux(d) == {"axis", "joint", "direction", "velocity"}

    def test_jog_increment_adds_distance(self):
        _btn, d = self._mk("jog_increment")
        assert self._visible_aux(d) == {
            "axis", "joint", "direction", "velocity", "distance",
        }

    def test_mdi_shows_only_mdi_line(self):
        _btn, d = self._mk("mdi")
        assert self._visible_aux(d) == {"mdi_line"}

    def test_load_program_shows_only_path(self):
        _btn, d = self._mk("load_program")
        assert self._visible_aux(d) == {"path"}

    def test_debug_shows_only_mask(self):
        _btn, d = self._mk("debug")
        assert self._visible_aux(d) == {"mask"}

    def test_spindle_shows_target_speed_index(self):
        _btn, d = self._mk("spindle")
        assert self._visible_aux(d) == {"target", "speed", "index"}

    def test_error_msg_shows_only_message(self):
        _btn, d = self._mk("error_msg")
        assert self._visible_aux(d) == {"message"}

    def test_flip_action_hides_and_shows_correct_rows(self):
        _btn, d = self._mk("estop")
        assert self._visible_aux(d) == set()
        for i in range(d._action_combo.count()):
            if d._action_combo.itemData(i) == "jog_increment":
                d._action_combo.setCurrentIndex(i)
                break
        assert self._visible_aux(d) == {
            "axis", "joint", "direction", "velocity", "distance",
        }
        for i in range(d._action_combo.count()):
            if d._action_combo.itemData(i) == "mdi":
                d._action_combo.setCurrentIndex(i)
                break
        assert self._visible_aux(d) == {"mdi_line"}

    def test_accept_writes_action_and_relevant_parameters(self):
        btn, d = self._mk("estop")
        for i in range(d._action_combo.count()):
            if d._action_combo.itemData(i) == "jog":
                d._action_combo.setCurrentIndex(i)
                break
        d._param_rows["axis"][1].setValue(4)
        d._param_rows["direction"][1].setValue(-1)
        d._param_rows["velocity"][1].setValue(77.25)
        d._param_rows["mdi_line"][1].setText("G0 X999")
        d.accept()
        assert btn.action.name == "jog"
        assert btn.axis == 4
        assert btn.direction == -1
        assert btn.velocity == pytest.approx(77.25)
        assert btn.mdi_line == ""

    def test_cancel_leaves_widget_untouched(self):
        btn, d = self._mk("jog", axis=1, velocity=10.0)
        for i in range(d._action_combo.count()):
            if d._action_combo.itemData(i) == "mdi":
                d._action_combo.setCurrentIndex(i)
                break
        d._param_rows["mdi_line"][1].setText("G1 X5")
        d.reject()
        assert btn.action.name == "jog"
        assert btn.axis == 1
        assert btn.velocity == pytest.approx(10.0)
        assert btn.mdi_line == ""

    def test_combo_contains_every_registered_action(self):
        from qtcnc.widgets.common.action_button import _VALID_ACTIONS
        _btn, d = self._mk("estop")
        combo_names = [
            d._action_combo.itemData(i)
            for i in range(d._action_combo.count())
        ]
        assert set(combo_names) == set(_VALID_ACTIONS)
        assert None not in combo_names
        assert combo_names == sorted(_VALID_ACTIONS)


class TestActionButtonTaskMenuFactory:
    """The factory hands Designer an extension only for ActionButton and
    only for the TaskMenuExtension IID."""

    def test_returns_extension_for_action_button(self):
        from qtcnc.designer.action_button_editor import (
            TASK_MENU_IID,
            ActionButtonTaskMenu,
            ActionButtonTaskMenuFactory,
        )
        from qtcnc.widgets.common.action_button import ActionButton
        factory = ActionButtonTaskMenuFactory()
        btn = ActionButton()
        ext = factory.createExtension(btn, TASK_MENU_IID, None)
        assert isinstance(ext, ActionButtonTaskMenu)

    def test_returns_none_for_wrong_iid(self):
        from qtcnc.designer.action_button_editor import ActionButtonTaskMenuFactory
        from qtcnc.widgets.common.action_button import ActionButton
        factory = ActionButtonTaskMenuFactory()
        assert factory.createExtension(
            ActionButton(), "org.qt-project.Qt.Designer.PropertySheet", None,
        ) is None

    def test_returns_none_for_non_action_button(self):
        from qtpy.QtWidgets import QPushButton
        from qtcnc.designer.action_button_editor import (
            TASK_MENU_IID,
            ActionButtonTaskMenuFactory,
        )
        factory = ActionButtonTaskMenuFactory()
        assert factory.createExtension(QPushButton(), TASK_MENU_IID, None) is None

    def test_task_menu_exposes_edit_action(self):
        from qtcnc.designer.action_button_editor import (
            TASK_MENU_IID,
            ActionButtonTaskMenuFactory,
        )
        from qtcnc.widgets.common.action_button import ActionButton
        factory = ActionButtonTaskMenuFactory()
        ext = factory.createExtension(ActionButton(), TASK_MENU_IID, None)
        assert ext is not None
        actions = ext.taskActions()
        assert len(actions) == 1
        assert actions[0].text() == "Edit Action…"
        assert ext.preferredEditAction() is actions[0]


class TestActionButtonPluginTaskMenuRegistration:
    """``ActionButtonPlugin.initialize`` registers a task-menu factory
    against Designer's extension manager exactly once."""

    def test_initialize_registers_task_menu_factory(self):
        from qtcnc.designer.action_button_editor import (
            TASK_MENU_IID,
            ActionButtonTaskMenuFactory,
        )
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        plugin = ActionButtonPlugin()
        form_editor = _FakeFormEditor(property_editor=_FakePropertyEditor())
        plugin.initialize(form_editor)
        registered = form_editor.extension_manager.registered
        assert len(registered) == 1
        factory, iid = registered[0]
        assert isinstance(factory, ActionButtonTaskMenuFactory)
        assert iid == TASK_MENU_IID

    def test_second_initialize_is_a_noop(self):
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        plugin = ActionButtonPlugin()
        form_editor = _FakeFormEditor(property_editor=_FakePropertyEditor())
        plugin.initialize(form_editor)
        plugin.initialize(form_editor)
        assert len(form_editor.extension_manager.registered) == 1

    def test_initialize_with_none_form_editor_does_not_crash(self):
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        plugin = ActionButtonPlugin()
        plugin.initialize(None)
        assert plugin.isInitialized() is True

    def test_initialize_connects_property_changed_signal(self):
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        plugin = ActionButtonPlugin()
        editor = _FakePropertyEditor()
        form_editor = _FakeFormEditor(property_editor=editor)
        plugin.initialize(form_editor)
        assert plugin._on_property_changed in editor.propertyChanged._slots


class TestApplyActionVisibility:
    """``apply_action_visibility`` flips the default property sheet's
    per-property visibility for auxiliary properties and prods the
    Property Editor to repaint."""

    def _aux_names(self) -> tuple[str, ...]:
        from qtcnc.designer.action_button_editor import _ALL_AUX_NAMES
        return tuple(sorted(_ALL_AUX_NAMES))

    def test_jog_hides_non_jog_aux_and_shows_jog_aux(self):
        from qtcnc.designer.action_button_editor import apply_action_visibility
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        sheet = _FakeSheet(self._aux_names())
        editor = _FakePropertyEditor()
        form_editor = _FakeFormEditor(property_editor=editor, sheet=sheet)
        form = _FakeFormWindow(form_editor)
        apply_action_visibility(
            form, btn, ActionButton.relevant_property_names("jog"),
        )
        visible = {k for k, v in sheet.visibility.items() if v}
        hidden = {k for k, v in sheet.visibility.items() if not v}
        assert visible == {"axis", "joint", "direction", "velocity"}
        assert "mdi_line" in hidden
        assert "path" in hidden
        assert "mask" in hidden

    def test_estop_hides_every_aux_property(self):
        from qtcnc.designer.action_button_editor import apply_action_visibility
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        sheet = _FakeSheet(self._aux_names())
        editor = _FakePropertyEditor()
        form = _FakeFormWindow(
            _FakeFormEditor(property_editor=editor, sheet=sheet),
        )
        apply_action_visibility(
            form, btn, ActionButton.relevant_property_names("estop"),
        )
        assert all(v is False for v in sheet.visibility.values())
        assert set(sheet.visibility) == set(self._aux_names())

    def test_mdi_shows_only_mdi_line(self):
        from qtcnc.designer.action_button_editor import apply_action_visibility
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        sheet = _FakeSheet(self._aux_names())
        form = _FakeFormWindow(
            _FakeFormEditor(property_editor=_FakePropertyEditor(), sheet=sheet),
        )
        apply_action_visibility(
            form, btn, ActionButton.relevant_property_names("mdi"),
        )
        visible = {k for k, v in sheet.visibility.items() if v}
        assert visible == {"mdi_line"}

    def test_prods_property_editor_to_repaint(self):
        from qtcnc.designer.action_button_editor import apply_action_visibility
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        editor = _FakePropertyEditor()
        form = _FakeFormWindow(
            _FakeFormEditor(
                property_editor=editor,
                sheet=_FakeSheet(self._aux_names()),
            ),
        )
        apply_action_visibility(
            form, btn, ActionButton.relevant_property_names("jog"),
        )
        assert editor.set_object_calls == [btn]

    def test_noop_when_extension_manager_returns_no_sheet(self):
        from qtcnc.designer.action_button_editor import apply_action_visibility
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        form = _FakeFormWindow(
            _FakeFormEditor(property_editor=_FakePropertyEditor(), sheet=None),
        )
        apply_action_visibility(
            form, btn, ActionButton.relevant_property_names("jog"),
        )

    def test_unknown_aux_name_on_sheet_is_skipped(self):
        from qtcnc.designer.action_button_editor import apply_action_visibility
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        sheet = _FakeSheet(("axis", "velocity"))
        form = _FakeFormWindow(
            _FakeFormEditor(property_editor=_FakePropertyEditor(), sheet=sheet),
        )
        apply_action_visibility(
            form, btn, ActionButton.relevant_property_names("jog"),
        )
        assert sheet.visibility == {"axis": True, "velocity": True}

    def test_action_property_is_always_hidden(self):
        from qtcnc.designer.action_button_editor import (
            _ALL_AUX_NAMES,
            apply_action_visibility,
        )
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        sheet = _FakeSheet(tuple(sorted(_ALL_AUX_NAMES)) + ("action",))
        form = _FakeFormWindow(
            _FakeFormEditor(property_editor=_FakePropertyEditor(), sheet=sheet),
        )
        apply_action_visibility(
            form, btn, ActionButton.relevant_property_names("jog"),
        )
        assert sheet.visibility["action"] is False

    def test_action_hidden_for_every_action_choice(self):
        from qtcnc.designer.action_button_editor import (
            _ALL_AUX_NAMES,
            apply_action_visibility,
        )
        from qtcnc.widgets.common.action_button import (
            ActionButton,
            _VALID_ACTIONS,
        )
        for action_name in _VALID_ACTIONS:
            btn = ActionButton()
            sheet = _FakeSheet(tuple(sorted(_ALL_AUX_NAMES)) + ("action",))
            form = _FakeFormWindow(
                _FakeFormEditor(
                    property_editor=_FakePropertyEditor(),
                    sheet=sheet,
                ),
            )
            apply_action_visibility(
                form, btn,
                ActionButton.relevant_property_names(action_name),
            )
            assert sheet.visibility["action"] is False, (
                f"action row must stay hidden for action={action_name!r}"
            )


class TestActionButtonPluginPropertyChangedHook:
    """``ActionButtonPlugin._on_property_changed`` re-applies aux visibility
    when the ``action`` property is edited on an ``ActionButton`` through
    Designer's property editor."""

    def _mk(self, plugin_cls: Any, sheet: _FakeSheet) -> tuple[Any, Any, Any]:
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        editor = _FakePropertyEditor()
        form_editor = _FakeFormEditor(property_editor=editor, sheet=sheet)
        plugin = plugin_cls()
        plugin.initialize(form_editor)
        editor.setObject(btn)
        return plugin, editor, btn

    def test_action_change_on_action_button_applies_visibility(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from qtcnc.designer import action_button_editor, qtcnc_widget_plugin
        from qtcnc.designer.action_button_editor import _ALL_AUX_NAMES
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        from qtcnc.widgets.common.action_button import ActionButton
        sheet = _FakeSheet(tuple(sorted(_ALL_AUX_NAMES)))
        plugin, editor, btn = self._mk(ActionButtonPlugin, sheet)
        btn._set_action("jog")
        captured: list[tuple[Any, Any, Any]] = []

        def _fake_find(w: Any) -> Any:
            return _FakeFormWindow(plugin._form_editor)

        def _spy(form: Any, w: Any, relevant: Any) -> None:
            captured.append((form, w, frozenset(relevant)))
            action_button_editor.apply_action_visibility(form, w, relevant)

        monkeypatch.setattr(
            qtcnc_widget_plugin.QDesignerFormWindowInterface,
            "findFormWindow",
            staticmethod(_fake_find),
        )
        monkeypatch.setattr(
            qtcnc_widget_plugin, "apply_action_visibility", _spy,
        )
        editor.propertyChanged.emit("action", "jog")
        assert len(captured) == 1
        _form, widget, relevant = captured[0]
        assert widget is btn
        assert relevant == ActionButton.relevant_property_names("jog")
        visible = {k for k, v in sheet.visibility.items() if v}
        assert visible == {"axis", "joint", "direction", "velocity"}

    def test_non_action_property_change_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from qtcnc.designer import qtcnc_widget_plugin
        from qtcnc.designer.action_button_editor import _ALL_AUX_NAMES
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        sheet = _FakeSheet(tuple(sorted(_ALL_AUX_NAMES)))
        plugin, editor, _btn = self._mk(ActionButtonPlugin, sheet)
        called: list[Any] = []
        monkeypatch.setattr(
            qtcnc_widget_plugin, "apply_action_visibility",
            lambda *a, **k: called.append(a),
        )
        editor.propertyChanged.emit("text", "foo")
        editor.propertyChanged.emit("enabled", True)
        assert called == []

    def test_action_change_on_non_action_button_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from qtpy.QtWidgets import QPushButton
        from qtcnc.designer import qtcnc_widget_plugin
        from qtcnc.designer.action_button_editor import _ALL_AUX_NAMES
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        sheet = _FakeSheet(tuple(sorted(_ALL_AUX_NAMES)))
        editor = _FakePropertyEditor()
        form_editor = _FakeFormEditor(property_editor=editor, sheet=sheet)
        plugin = ActionButtonPlugin()
        plugin.initialize(form_editor)
        editor.setObject(QPushButton())
        called: list[Any] = []
        monkeypatch.setattr(
            qtcnc_widget_plugin, "apply_action_visibility",
            lambda *a, **k: called.append(a),
        )
        editor.propertyChanged.emit("action", "jog")
        assert called == []


class TestActionButtonPluginFormWindowHook:
    """``ActionButtonPlugin`` walks the form window manager at init time
    and hooks new form windows so every ``ActionButton`` that enters
    Designer gets its aux visibility applied — covering drop-from-palette
    (via ``widgetManaged``) and form-loaded-from-disk (via the initial
    walk of ``mainContainer.findChildren``)."""

    def _sheet_names(self) -> tuple[str, ...]:
        from qtcnc.designer.action_button_editor import _ALL_AUX_NAMES
        return tuple(sorted(_ALL_AUX_NAMES)) + ("action",)

    def test_initialize_connects_form_window_added(self):
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        plugin = ActionButtonPlugin()
        fw_manager = _FakeFormWindowManager()
        form_editor = _FakeFormEditor(
            property_editor=_FakePropertyEditor(),
            form_window_manager=fw_manager,
        )
        plugin.initialize(form_editor)
        assert plugin._on_form_window_added in fw_manager.formWindowAdded._slots

    def test_initialize_walks_existing_form_windows(self):
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        btn._set_action("jog")
        sheet = _FakeSheet(self._sheet_names())
        form_editor = _FakeFormEditor(
            property_editor=_FakePropertyEditor(),
            sheet=sheet,
        )
        container = _FakeContainer([btn])
        form = _FakeFormWindow(form_editor, main_container=container)
        fw_manager = _FakeFormWindowManager(form_windows=[form])
        form_editor._form_window_manager = fw_manager
        plugin = ActionButtonPlugin()
        plugin.initialize(form_editor)
        visible = {k for k, v in sheet.visibility.items() if v}
        assert visible == {"axis", "joint", "direction", "velocity"}
        assert sheet.visibility["action"] is False

    def test_form_window_added_signal_hides_action_on_existing_widgets(self):
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        from qtcnc.widgets.common.action_button import ActionButton
        btn = ActionButton()
        btn._set_action("mdi")
        sheet = _FakeSheet(self._sheet_names())
        form_editor = _FakeFormEditor(
            property_editor=_FakePropertyEditor(),
            sheet=sheet,
            form_window_manager=_FakeFormWindowManager(),
        )
        container = _FakeContainer([btn])
        form = _FakeFormWindow(form_editor, main_container=container)
        plugin = ActionButtonPlugin()
        plugin.initialize(form_editor)
        form_editor._form_window_manager.formWindowAdded.emit(form)
        visible = {k for k, v in sheet.visibility.items() if v}
        assert visible == {"mdi_line"}
        assert sheet.visibility["action"] is False

    def test_form_window_added_connects_widget_managed(self):
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        form_editor = _FakeFormEditor(
            property_editor=_FakePropertyEditor(),
            form_window_manager=_FakeFormWindowManager(),
        )
        form = _FakeFormWindow(
            form_editor, main_container=_FakeContainer([]),
        )
        plugin = ActionButtonPlugin()
        plugin.initialize(form_editor)
        form_editor._form_window_manager.formWindowAdded.emit(form)
        assert len(form.widgetManaged._slots) == 1

    def test_widget_managed_applies_visibility_to_dropped_action_button(self):
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        from qtcnc.widgets.common.action_button import ActionButton
        sheet = _FakeSheet(self._sheet_names())
        form_editor = _FakeFormEditor(
            property_editor=_FakePropertyEditor(),
            sheet=sheet,
            form_window_manager=_FakeFormWindowManager(),
        )
        form = _FakeFormWindow(
            form_editor, main_container=_FakeContainer([]),
        )
        plugin = ActionButtonPlugin()
        plugin.initialize(form_editor)
        form_editor._form_window_manager.formWindowAdded.emit(form)
        btn = ActionButton()
        btn._set_action("load_program")
        form.widgetManaged.emit(btn)
        visible = {k for k, v in sheet.visibility.items() if v}
        assert visible == {"path"}
        assert sheet.visibility["action"] is False

    def test_widget_managed_ignores_non_action_buttons(self):
        from qtpy.QtWidgets import QPushButton
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        sheet = _FakeSheet(self._sheet_names())
        form_editor = _FakeFormEditor(
            property_editor=_FakePropertyEditor(),
            sheet=sheet,
            form_window_manager=_FakeFormWindowManager(),
        )
        form = _FakeFormWindow(
            form_editor, main_container=_FakeContainer([]),
        )
        plugin = ActionButtonPlugin()
        plugin.initialize(form_editor)
        form_editor._form_window_manager.formWindowAdded.emit(form)
        form.widgetManaged.emit(QPushButton())
        assert sheet.visibility == {}

    def test_initialize_without_form_window_manager_does_not_crash(self):
        from qtcnc.designer.qtcnc_widget_plugin import ActionButtonPlugin
        plugin = ActionButtonPlugin()
        form_editor = _FakeFormEditor(property_editor=_FakePropertyEditor())
        plugin.initialize(form_editor)
        assert plugin.isInitialized() is True


class TestLauncherHelpers:
    def test_list_screens_finds_minimal_and_standard(self):
        from qtcnc.designer_launcher import _list_screens
        names = {p.name for p in _list_screens()}
        assert "minimal" in names
        assert "standard" in names

    def test_resolve_screen_returns_main_ui(self):
        from qtcnc.designer_launcher import _resolve_screen
        path = _resolve_screen("standard")
        assert path.name == "main.ui"
        assert path.is_file()

    def test_resolve_screen_unknown_raises(self):
        from qtcnc.designer_launcher import _resolve_screen
        with pytest.raises(FileNotFoundError):
            _resolve_screen("definitely_not_a_screen_name")

    def test_build_env_sets_pyqt_paths(self):
        from qtcnc.designer_launcher import _build_env, _DESIGNER_DIR, _LIB_PYTHON
        env = _build_env("pyqt5")
        assert env["QT_API"] == "pyqt5"
        assert env["QT_SELECT"] == "qt5"
        assert env["DESIGNER"] == "1"
        assert str(_DESIGNER_DIR) in env["PYQTDESIGNERPATH"].split(os.pathsep)
        assert str(_LIB_PYTHON) in env["PYTHONPATH"].split(os.pathsep)

    def test_build_env_pyqt6_sets_qt_select_qt6(self):
        from qtcnc.designer_launcher import _build_env
        env = _build_env("pyqt6")
        assert env["QT_API"] == "pyqt6"
        assert env["QT_SELECT"] == "qt6"
