"""Qt Designer plugin registrations for every shipped qtcnc widget.

Designer's ``libpyqt6.so`` scans ``PYQTDESIGNERPATH`` directories and
only imports files whose basename ends in ``_plugin.py`` — note the
singular ``plugin`` — so this file is named to match. Each plugin
class below subclasses :class:`_QtcncDesignerPlugin` and overrides
:meth:`pluginClass` plus, for widgets with Qt properties,
:meth:`custom_properties` so the base class can assemble a domXml
snippet that pre-declares every property with ``stdset="0"``.

``ActionButtonPlugin`` additionally registers a
``QPyDesignerTaskMenuExtension`` factory so right-clicking an
``ActionButton`` in Designer offers "Edit Action…", which opens a
parameter dialog scoped to the chosen action. All auxiliary Qt
properties stay visible in the Property Editor at all times.

Adding a new widget: import its class, add a ``class FooPlugin`` block
at the bottom, and fill in ``custom_properties`` with every
Designer-visible Qt property so the property editor shows them after
drop.
"""

from __future__ import annotations

from typing import Any

from qtpy.QtDesigner import QDesignerFormWindowInterface

from qtcnc.designer import _plugin_base as _base
from qtcnc.designer.action_button_editor import (
    TASK_MENU_IID,
    ActionButtonTaskMenuFactory,
    _debug,
    apply_action_visibility,
)
from qtcnc.widgets.common.action_button import ActionButton
from qtcnc.widgets.common.dro_grid import DroGrid
from qtcnc.widgets.common.gcode_preview import GcodePreview
from qtcnc.widgets.common.gcode_view import GcodeView
from qtcnc.widgets.common.mdi_entry import MdiEntry
from qtcnc.widgets.common.override_slider import OverrideSlider
from qtcnc.widgets.common.state_label import StateLabel
from qtcnc.widgets.common.tool_offset_view import ToolOffsetView


class ActionButtonPlugin(_base._QtcncDesignerPlugin):
    def pluginClass(self) -> type:
        return ActionButton

    def toolTip(self) -> str:
        return "Button bound to a Command verb (estop, jog, mdi, …)"

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._form_editor: Any = None
        self._task_menu_factory: Any = None

    def initialize(self, form_editor: Any) -> None:
        _debug(f"ActionButtonPlugin.initialize form_editor={form_editor!r}")
        if self._initialized:
            _debug("ActionButtonPlugin.initialize already initialized")
            return
        self._form_editor = form_editor
        if form_editor is not None:
            try:
                manager = form_editor.extensionManager()
            except Exception as e:
                _debug(f"extensionManager() raised {e!r}")
                manager = None
            _debug(f"extensionManager -> {manager!r}")
            if manager is not None:
                self._task_menu_factory = ActionButtonTaskMenuFactory(manager)
                manager.registerExtensions(
                    self._task_menu_factory, TASK_MENU_IID,
                )
                _debug(f"registerExtensions({TASK_MENU_IID!r}) done")
            try:
                editor_panel = form_editor.propertyEditor()
            except Exception as e:
                _debug(f"propertyEditor() raised {e!r}")
                editor_panel = None
            if editor_panel is not None:
                try:
                    editor_panel.propertyChanged.connect(
                        self._on_property_changed,
                    )
                    _debug("connected propertyEditor.propertyChanged")
                except Exception as e:
                    _debug(f"propertyChanged.connect raised {e!r}")
            try:
                fw_manager = form_editor.formWindowManager()
            except Exception as e:
                _debug(f"formWindowManager() raised {e!r}")
                fw_manager = None
            if fw_manager is not None:
                try:
                    fw_manager.formWindowAdded.connect(
                        self._on_form_window_added,
                    )
                    _debug("connected formWindowManager.formWindowAdded")
                except Exception as e:
                    _debug(f"formWindowAdded.connect raised {e!r}")
                try:
                    count = int(fw_manager.formWindowCount())
                except Exception as e:
                    _debug(f"formWindowCount() raised {e!r}")
                    count = 0
                for i in range(count):
                    try:
                        existing = fw_manager.formWindow(i)
                    except Exception as e:
                        _debug(f"formWindow({i}) raised {e!r}")
                        continue
                    if existing is not None:
                        self._on_form_window_added(existing)
        super().initialize(form_editor)

    def _on_property_changed(self, name: str, _value: Any) -> None:
        if name != "action":
            return
        editor_panel = None
        try:
            editor_panel = self._form_editor.propertyEditor()
        except Exception:
            return
        if editor_panel is None:
            return
        try:
            obj = editor_panel.object()
        except Exception:
            return
        if not isinstance(obj, ActionButton):
            return
        form = QDesignerFormWindowInterface.findFormWindow(obj)
        if form is None:
            return
        relevant = ActionButton.relevant_property_names(obj.action)
        apply_action_visibility(form, obj, relevant)

    def _on_form_window_added(self, form: Any) -> None:
        _debug(f"_on_form_window_added form={form!r}")
        try:
            form.widgetManaged.connect(
                lambda w, f=form: self._on_widget_managed(f, w),
            )
            _debug("connected form.widgetManaged")
        except Exception as e:
            _debug(f"widgetManaged.connect raised {e!r}")
        try:
            container = form.mainContainer()
        except Exception as e:
            _debug(f"form.mainContainer() raised {e!r}")
            container = None
        if container is not None:
            try:
                existing = container.findChildren(ActionButton)
            except Exception as e:
                _debug(f"findChildren(ActionButton) raised {e!r}")
                existing = []
            for widget in existing:
                self._apply_visibility(form, widget)

    def _on_widget_managed(self, form: Any, widget: Any) -> None:
        if not isinstance(widget, ActionButton):
            return
        self._apply_visibility(form, widget)

    def _apply_visibility(self, form: Any, widget: ActionButton) -> None:
        try:
            relevant = ActionButton.relevant_property_names(widget.action)
        except Exception as e:
            _debug(f"relevant_property_names raised {e!r}")
            return
        apply_action_visibility(form, widget, relevant)

    def custom_properties(self) -> list[tuple[str, str, Any]]:
        return [
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
        ]


class StateLabelPlugin(_base._QtcncDesignerPlugin):
    def pluginClass(self) -> type:
        return StateLabel

    def toolTip(self) -> str:
        return "Label that displays one slice of machine state"

    def custom_properties(self) -> list[tuple[str, str, Any]]:
        return [
            ("state", "string", "task_state"),
            ("axis", "number", 0),
            ("index", "number", 0),
            ("format_string", "string", ""),
        ]


class DroGridPlugin(_base._QtcncDesignerPlugin):
    def pluginClass(self) -> type:
        return DroGrid

    def toolTip(self) -> str:
        return "Multi-axis position grid (work / machine / dtg)"

    def custom_properties(self) -> list[tuple[str, str, Any]]:
        return [
            ("coord_system", "string", "work"),
            ("format_string", "string", ""),
        ]


class OverrideSliderPlugin(_base._QtcncDesignerPlugin):
    def pluginClass(self) -> type:
        return OverrideSlider

    def toolTip(self) -> str:
        return "Feed / rapid / spindle override slider"

    def custom_properties(self) -> list[tuple[str, str, Any]]:
        return [
            ("kind", "string", "feed"),
            ("index", "number", 0),
            ("max_value", "double", 1.5),
        ]


class GcodeViewPlugin(_base._QtcncDesignerPlugin):
    def pluginClass(self) -> type:
        return GcodeView

    def toolTip(self) -> str:
        return "Read-only g-code text view with current-line highlight"


class GcodePreviewPlugin(_base._QtcncDesignerPlugin):
    def pluginClass(self) -> type:
        return GcodePreview

    def toolTip(self) -> str:
        return "Toolpath preview rendered from the loaded program"


class MdiEntryPlugin(_base._QtcncDesignerPlugin):
    def pluginClass(self) -> type:
        return MdiEntry

    def toolTip(self) -> str:
        return "Line edit that submits its text as an MDI command"

    def custom_properties(self) -> list[tuple[str, str, Any]]:
        return [
            ("submit_on_enter", "bool", True),
            ("clear_on_submit", "bool", True),
        ]


class ToolOffsetViewPlugin(_base._QtcncDesignerPlugin):
    def pluginClass(self) -> type:
        return ToolOffsetView

    def toolTip(self) -> str:
        return "Editable table of tool offsets backed by the daemon's tool DB"
