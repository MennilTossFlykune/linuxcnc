"""Right-click "Edit Action…" dialog for ActionButton in Qt Designer.

Designer's default property sheet for ``ActionButton`` lists every
auxiliary property at all times, because mutating per-property
visibility from PyQt runs into marshalling issues with
``QDesignerPropertySheetExtension``. The task-menu extension path is
well-supported: ``QPyDesignerTaskMenuExtension`` adds one or more
``QAction`` entries to the widget's right-click menu inside Designer,
and any action can open a Python-owned ``QDialog`` that mutates the
selected widget through ``widget.setProperty``.

``ActionButtonEditorDialog`` presents the action picker grouped by
category plus a parameter pane that only shows editors for the
properties the chosen action consumes. The mapping comes from
:meth:`ActionButton.relevant_property_names`, so adding a new verb in
``action_button.py`` automatically surfaces its parameters here.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from qtpy import sip
from qtpy.QtDesigner import (
    QDesignerFormWindowInterface,
    QDesignerPropertyEditorInterface,
    QDesignerPropertySheetExtension,
    QExtensionFactory,
    QPyDesignerTaskMenuExtension,
)
from qtpy.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qtcnc.widgets.common.action_button import (
    _VALID_ACTIONS,
    ActionButton,
    ActionVerb,
)


TASK_MENU_IID = "org.qt-project.Qt.Designer.TaskMenu"
PROPERTY_SHEET_IID = "org.qt-project.Qt.Designer.PropertySheet"


def _sip_cast(obj: Any, target_cls: Any) -> Any:
    """Return ``obj`` unchanged.

    The ``action`` combobox and every auxiliary ActionButton
    property are hidden from Designer's Property Editor at the
    class level via ``designable=False`` on the Qt ``Property``
    declarations. The right-click "Edit Action…" dialog remains the
    edit surface; no runtime property-sheet manipulation is needed.
    """
    return obj


_log = logging.getLogger("qtcnc.designer")


def _debug(msg: str) -> None:
    if not os.environ.get("QTCNC_DESIGNER_DEBUG"):
        return
    _log.debug(msg)
    try:
        with open("/tmp/qtcnc-designer-debug.log", "a") as fh:
            fh.write(f"[qtcnc-designer] {msg}\n")
    except Exception:
        pass


_INT_PROPS: frozenset[str] = frozenset({"axis", "joint", "direction", "index", "mask"})
_FLOAT_PROPS: frozenset[str] = frozenset({"velocity", "distance", "speed", "value"})
_STRING_PROPS: frozenset[str] = frozenset({"target", "mdi_line", "path", "message"})
_BOOL_PROPS: frozenset[str] = frozenset({"confirm"})

_ALL_AUX_NAMES: frozenset[str] = (
    _INT_PROPS | _FLOAT_PROPS | _STRING_PROPS | _BOOL_PROPS
)

_ALWAYS_HIDDEN: frozenset[str] = frozenset({"action"})


def apply_action_visibility(
    form: Any, widget: ActionButton, relevant: frozenset[str],
) -> None:
    """Hide the action combobox and every aux property the action doesn't consume.

    The `action` property is always hidden from Designer's property
    editor — the right-click "Edit Action…" dialog is the only entry
    point for changing it. Auxiliary properties (target, axis, …) are
    hidden unless the chosen action is named in ``relevant``. Silently
    no-ops when the extension manager, sheet, or editor is unavailable.
    """
    try:
        core = form.core()
    except Exception as e:
        _debug(f"form.core() raised {e!r}")
        return
    if core is None:
        return
    try:
        manager = core.extensionManager()
    except Exception as e:
        _debug(f"core.extensionManager() raised {e!r}")
        return
    if manager is None:
        return
    try:
        sheet = manager.extension(widget, PROPERTY_SHEET_IID)
    except Exception as e:
        _debug(f"manager.extension(property sheet) raised {e!r}")
        return
    if sheet is None:
        _debug("property sheet extension is None")
        return
    sheet = _sip_cast(sheet, QDesignerPropertySheetExtension)

    def _set_visible(name: str, visible: bool) -> None:
        try:
            idx = sheet.indexOf(name)
        except Exception as e:
            _debug(f"sheet.indexOf({name!r}) raised {e!r}")
            return
        if idx < 0:
            return
        try:
            sheet.setVisible(idx, visible)
        except Exception as e:
            _debug(f"sheet.setVisible({name!r}) raised {e!r}")

    for hidden_name in _ALWAYS_HIDDEN:
        _set_visible(hidden_name, False)
    for aux_name in _ALL_AUX_NAMES:
        _set_visible(aux_name, aux_name in relevant)

    try:
        editor_panel = core.propertyEditor()
    except Exception:
        editor_panel = None
    if editor_panel is not None:
        editor_panel = _sip_cast(editor_panel, QDesignerPropertyEditorInterface)
        try:
            editor_panel.setObject(widget)
        except Exception as e:
            _debug(f"propertyEditor.setObject raised {e!r}")

_AUX_LABELS: dict[str, str] = {
    "target": "Target",
    "axis": "Axis",
    "direction": "Direction",
    "velocity": "Velocity",
    "distance": "Distance",
    "speed": "Speed",
    "index": "Index",
    "mdi_line": "MDI line",
    "path": "Path",
    "value": "Value",
    "message": "Message",
    "mask": "Mask",
    "confirm": "Confirm",
}


class ActionButtonEditorDialog(QDialog):
    """Modal editor for an ``ActionButton`` instance inside Designer.

    The dialog seeds its editors from the widget's current properties,
    hides parameter rows that the chosen action does not consume, and
    on ``OK`` writes every relevant property back to the widget via
    ``setProperty``. Cancel leaves the widget untouched.
    """

    def __init__(self, widget: ActionButton, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._widget = widget
        self.setWindowTitle("Edit ActionButton")
        self.setModal(True)

        self._action_combo = QComboBox(self)
        self._build_action_combo()

        self._param_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._form_layout = QFormLayout()
        self._build_param_editors()

        layout = QVBoxLayout(self)
        top_form = QFormLayout()
        top_form.addRow("Action", self._action_combo)
        layout.addLayout(top_form)
        layout.addLayout(self._form_layout)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self,
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._seed_from_widget()
        self._action_combo.currentIndexChanged.connect(self._on_action_changed)
        self._refresh_visible_rows()

    # ---- combo / editors ---------------------------------------------------

    def _build_action_combo(self) -> None:
        for name in sorted(_VALID_ACTIONS):
            self._action_combo.addItem(name, userData=name)

    def _build_param_editors(self) -> None:
        for name in sorted(ActionButton.all_auxiliary_property_names()):
            label = QLabel(_AUX_LABELS.get(name, name), self)
            editor = self._make_editor(name)
            self._form_layout.addRow(label, editor)
            self._param_rows[name] = (label, editor)

    def _make_editor(self, name: str) -> QWidget:
        if name in _INT_PROPS:
            box = QSpinBox(self)
            box.setRange(-1_000_000, 1_000_000)
            return box
        if name in _FLOAT_PROPS:
            box = QDoubleSpinBox(self)
            box.setRange(-1e9, 1e9)
            box.setDecimals(4)
            return box
        if name in _BOOL_PROPS:
            return QCheckBox(self)
        if name in _STRING_PROPS:
            return QLineEdit(self)
        return QLineEdit(self)

    # ---- seeding from widget ----------------------------------------------

    def _current_action_name(self) -> str:
        verb = self._widget.action
        if isinstance(verb, ActionVerb):
            return verb.name
        if isinstance(verb, str):
            return verb
        try:
            return ActionVerb(int(verb)).name
        except (ValueError, TypeError):
            return "estop"

    def _seed_from_widget(self) -> None:
        action_name = self._current_action_name()
        for i in range(self._action_combo.count()):
            if self._action_combo.itemData(i) == action_name:
                self._action_combo.setCurrentIndex(i)
                break
        for name, (_label, editor) in self._param_rows.items():
            value = self._widget.property(name)
            self._write_editor(editor, name, value)

    def _write_editor(self, editor: QWidget, name: str, value: Any) -> None:
        if name in _INT_PROPS and isinstance(editor, QSpinBox):
            editor.setValue(int(value or 0))
        elif name in _FLOAT_PROPS and isinstance(editor, QDoubleSpinBox):
            editor.setValue(float(value or 0.0))
        elif name in _BOOL_PROPS and isinstance(editor, QCheckBox):
            editor.setChecked(bool(value))
        elif isinstance(editor, QLineEdit):
            editor.setText("" if value is None else str(value))

    def _read_editor(self, editor: QWidget, name: str) -> Any:
        if name in _INT_PROPS and isinstance(editor, QSpinBox):
            return int(editor.value())
        if name in _FLOAT_PROPS and isinstance(editor, QDoubleSpinBox):
            return float(editor.value())
        if name in _BOOL_PROPS and isinstance(editor, QCheckBox):
            return editor.isChecked()
        if isinstance(editor, QLineEdit):
            return editor.text()
        return None

    # ---- visibility --------------------------------------------------------

    def _selected_action_name(self) -> str | None:
        return self._action_combo.currentData()

    def _refresh_visible_rows(self) -> None:
        action_name = self._selected_action_name()
        if action_name is None:
            for label, editor in self._param_rows.values():
                label.setVisible(False)
                editor.setVisible(False)
            return
        relevant = ActionButton.relevant_property_names(action_name)
        for name, (label, editor) in self._param_rows.items():
            show = name in relevant
            label.setVisible(show)
            editor.setVisible(show)

    def _on_action_changed(self, _index: int) -> None:
        self._refresh_visible_rows()

    # ---- dialog API --------------------------------------------------------

    def accept(self) -> None:
        action_name = self._selected_action_name()
        if action_name is None:
            return
        form = QDesignerFormWindowInterface.findFormWindow(self._widget)

        def _set(name: str, value: Any) -> None:
            if form is not None:
                form.cursor().setProperty(name, value)
            else:
                self._widget.setProperty(name, value)

        _set("action", action_name)
        relevant = ActionButton.relevant_property_names(action_name)
        for name in sorted(relevant):
            if name == "action" or name not in self._param_rows:
                continue
            _label, editor = self._param_rows[name]
            _set(name, self._read_editor(editor, name))

        if form is not None:
            apply_action_visibility(form, self._widget, relevant)

        super().accept()


class ActionButtonTaskMenu(QPyDesignerTaskMenuExtension):
    """Right-click menu extension adding "Edit Action…" on ActionButton."""

    def __init__(self, widget: ActionButton, parent: QWidget | None = None) -> None:
        super(QPyDesignerTaskMenuExtension, self).__init__(parent)
        self._widget = widget
        self._edit_action = QAction(self.tr("Edit Action…"), self)
        self._edit_action.triggered.connect(self._open_dialog)

    def preferredEditAction(self) -> QAction:
        return self._edit_action

    def taskActions(self) -> list[QAction]:
        return [self._edit_action]

    def _open_dialog(self) -> None:
        dialog = ActionButtonEditorDialog(self._widget)
        dialog.exec_()


class ActionButtonTaskMenuFactory(QExtensionFactory):
    """Hands Designer a task-menu extension for every ``ActionButton``."""

    def __init__(self, parent: Any = None) -> None:
        try:
            super().__init__(parent)
        except TypeError:
            super().__init__(None)

    def createExtension(self, obj: Any, iid: str, parent: Any) -> Any:
        _debug(f"createExtension iid={iid!r} obj={type(obj).__name__}")
        if iid != TASK_MENU_IID:
            return None
        if not isinstance(obj, ActionButton):
            return None
        _debug(f"createExtension -> ActionButtonTaskMenu for {obj.objectName()!r}")
        return ActionButtonTaskMenu(obj, parent)
