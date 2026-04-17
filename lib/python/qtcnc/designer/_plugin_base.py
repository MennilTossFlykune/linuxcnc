"""Common base class for every qtcnc Qt Designer custom-widget plugin.

Each concrete plugin class subclasses :class:`_QtcncDesignerPlugin` and
overrides :meth:`pluginClass`, usually :meth:`toolTip`, and — for every
widget that exposes Qt Properties — :meth:`custom_properties`. The base
builds ``domXml`` from that list so every qtcnc property is emitted as
a ``<property ... stdset="0">`` entry; Designer both pre-populates the
value at drop time and surfaces the property in its property editor.

Designer loads this module inside its embedded Python interpreter, so
the only imports allowed here are stdlib, ``qtpy``, and pure-Python
widget modules (no transport/zmq, no ``linuxcnc``/``hal``).
"""

from __future__ import annotations

import os
from typing import Any

from qtpy.QtDesigner import QPyDesignerCustomWidgetPlugin
from qtpy.QtGui import QIcon


os.environ.setdefault("DESIGNER", "1")


_CustomProperty = tuple[str, str, Any]


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _format_value(type_name: str, value: Any) -> tuple[str, str]:
    """Return ``(xml_tag, xml_text)`` for a custom-property default."""
    if type_name == "string":
        return "string", _xml_escape(str(value))
    if type_name == "bool":
        return "bool", "true" if bool(value) else "false"
    if type_name == "number":
        return "number", str(int(value))
    if type_name == "double":
        return "double", repr(float(value))
    raise ValueError(f"unsupported custom-property type {type_name!r}")


class _QtcncDesignerPlugin(QPyDesignerCustomWidgetPlugin):
    """Base class that every qtcnc widget plugin inherits from."""

    group_name: str = "qtcnc"

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._initialized = False

    def pluginClass(self) -> type:
        raise NotImplementedError("subclass must override pluginClass()")

    def custom_properties(self) -> list[_CustomProperty]:
        """Declarative list of the widget's Qt properties.

        Entries are ``(name, type_tag, default)`` where ``type_tag`` is
        one of ``"string"``, ``"bool"``, ``"number"`` (int), or
        ``"double"`` (float). Every entry is emitted into ``domXml``
        with ``stdset="0"`` so Designer both pre-populates a default
        and shows the property in its property editor after drop.
        """
        return []

    def initialize(self, form_editor: Any) -> None:
        if self._initialized:
            return
        self._initialized = True

    def isInitialized(self) -> bool:
        return self._initialized

    def createWidget(self, parent: Any) -> Any:
        cls = self.pluginClass()
        return cls(parent)

    def name(self) -> str:
        return self.pluginClass().__name__

    def group(self) -> str:
        return self.group_name

    def icon(self) -> QIcon:
        return QIcon()

    def toolTip(self) -> str:
        return ""

    def whatsThis(self) -> str:
        return ""

    def isContainer(self) -> bool:
        return False

    def domXml(self) -> str:
        cls_name = self.name()
        obj_name = cls_name[0].lower() + cls_name[1:]
        parts: list[str] = [
            f'<widget class="{cls_name}" name="{obj_name}">\n',
            ' <property name="objectName">\n',
            f'  <string>{obj_name}</string>\n',
            ' </property>\n',
        ]
        for prop_name, type_name, default in self.custom_properties():
            tag, text = _format_value(type_name, default)
            parts.append(f' <property name="{prop_name}" stdset="0">\n')
            parts.append(f'  <{tag}>{text}</{tag}>\n')
            parts.append(' </property>\n')
        parts.append('</widget>\n')
        return "".join(parts)

    def includeFile(self) -> str:
        return self.pluginClass().__module__
