"""GcodePreview: 2D top-down toolpath renderer for the loaded program.

Built on `QGraphicsView`/`QGraphicsScene` rather than `QOpenGLWidget`
because:

* Headless pytest-qt runs — GL context creation is unreliable in CI
* The preview is a supporting widget, not the main toolpath renderer
* Swapping to a GL backend later is mechanical: the public API
  (`load_file`, `clear`, `set_current_line`) stays identical

Rendering model: one `QGraphicsLineItem` per segment.
Colours: rapid = light gray dashed, feed = dark blue, arc = teal,
probe = orange, rigid_tap = purple. Current-line highlight overlays a
yellow stroke on the segments whose `line` matches.

Performance: a segment ceiling of 500 000 (from the v2 plan's risk
register) triggers an on-screen "preview disabled" overlay instead of
attempting to render. Below that we rely on Qt's scene BSP indexing.
"""

from __future__ import annotations

from typing import Any, Optional

from qtpy.QtCore import QRectF, Qt
from qtpy.QtGui import QBrush, QColor, QPainter, QPen
from qtpy.QtWidgets import (
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from qtcnc.core.program import (
    GcodeParseError,
    GcodeProgram,
    ToolpathSegment,
    parse,
)
from qtcnc.core.types import ProgramState
from qtcnc.widgets.base import QtcncWidget


_MAX_SEGMENTS = 500_000

# Per-kind base pen colours. `arc_feed` keeps its own hue so the user can
# visually distinguish tessellated arcs from plain linear feeds.
_COLORS: dict[str, QColor] = {
    "rapid": QColor(180, 180, 180),
    "feed": QColor(40, 80, 200),
    "arc": QColor(0, 140, 160),
    "probe": QColor(220, 120, 0),
    "rigid_tap": QColor(140, 60, 180),
}
_CURRENT_LINE_COLOR = QColor(240, 220, 0)


class GcodePreview(QtcncWidget, QGraphicsView):
    """Top-down preview of the toolpath of the currently loaded program."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse,
        )
        # Qt's default Y axis runs top-down; flip it so +Y goes up like
        # a machinist expects. We apply this once and let users pan/zoom
        # on top of it.
        self.scale(1.0, -1.0)

        self._program: Optional[GcodeProgram] = None
        self._loaded_path: str = ""
        self._current_line: int = 0
        # Map of line number -> list of QGraphicsLineItem so the highlight
        # can be applied and rolled back cheaply.
        self._items_by_line: dict[int, list[QGraphicsLineItem]] = {}
        self._original_pens: dict[QGraphicsLineItem, QPen] = {}
        self._overlay_item: Optional[QGraphicsSimpleTextItem] = None

    # ----- lifecycle -----

    def qtcnc_setup(self) -> None:
        self.connect_status("program_loaded", self._on_program_loaded)
        self.connect_status("program_line_changed", self._on_line_changed)
        self.connect_status("program_closed", self._on_program_closed)
        state = self.window().qtcnc_status.state
        if state.program.path:
            self._on_program_loaded(state.program.path, state.program)
            if state.program.current_line > 0:
                self.set_current_line(state.program.current_line)

    # ----- public API -----

    def load_file(self, path: str) -> None:
        """Parse `path` and rebuild the scene. Empty path clears."""
        self.clear()
        if not path:
            return
        try:
            program = parse(path)
        except GcodeParseError as e:
            self._show_overlay(f"parse failed: {e}")
            return
        self.load_program(program)
        # Set after `load_program()` runs — it calls `clear()` which
        # resets `_loaded_path`, so this has to happen last.
        self._loaded_path = path

    def load_program(self, program: GcodeProgram) -> None:
        """Render a pre-parsed `GcodeProgram`. Used by handlers that
        already parsed the file (and by unit tests that bypass disk)."""
        self.clear()
        self._program = program
        if len(program.segments) > _MAX_SEGMENTS:
            self._show_overlay(
                f"preview disabled: {len(program.segments)} segments "
                f"(cap = {_MAX_SEGMENTS})"
            )
            return
        self._build_items(program.segments)
        if program.extents.is_empty:
            self._scene.setSceneRect(QRectF(-10, -10, 20, 20))
        else:
            ext = program.extents
            rect = QRectF(
                float(ext.min.x), float(ext.min.y),
                float(ext.max.x - ext.min.x),
                float(ext.max.y - ext.min.y),
            )
            margin = max(rect.width(), rect.height(), 1.0) * 0.1
            rect.adjust(-margin, -margin, margin, margin)
            self._scene.setSceneRect(rect)
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
            # `fitInView` wipes the Y-flip we set in __init__. Re-apply
            # so +Y is still "up" after the initial framing.
            tx = self.transform()
            if tx.m22() > 0:
                self.scale(1.0, -1.0)

    def clear(self) -> None:
        """Remove all items from the scene."""
        self._scene.clear()
        self._items_by_line.clear()
        self._original_pens.clear()
        self._overlay_item = None
        self._program = None
        self._loaded_path = ""
        self._current_line = 0

    def set_current_line(self, line: int) -> None:
        """Highlight segments emitted from source line `line` (1-indexed)."""
        # Roll back previous highlight first.
        if self._current_line and self._current_line in self._items_by_line:
            for item in self._items_by_line[self._current_line]:
                pen = self._original_pens.get(item)
                if pen is not None:
                    item.setPen(pen)
        self._current_line = int(line)
        if line <= 0:
            return
        for item in self._items_by_line.get(line, ()):
            pen = QPen(_CURRENT_LINE_COLOR)
            pen.setWidthF(self._highlight_width())
            pen.setCosmetic(True)
            item.setPen(pen)

    @property
    def loaded_path(self) -> str:
        return self._loaded_path

    @property
    def program(self) -> Optional[GcodeProgram]:
        return self._program

    @property
    def current_line(self) -> int:
        return self._current_line

    @property
    def segment_count(self) -> int:
        return len(self._program.segments) if self._program is not None else 0

    # ----- internals -----

    def _on_program_loaded(self, path: str, _program: ProgramState) -> None:
        self.load_file(path)

    def _on_program_closed(self) -> None:
        self.clear()

    def _on_line_changed(self, line: int) -> None:
        self.set_current_line(line)

    def _build_items(self, segments: tuple[ToolpathSegment, ...]) -> None:
        base_pens: dict[str, QPen] = {}
        for kind, color in _COLORS.items():
            pen = QPen(color)
            pen.setCosmetic(True)
            pen.setWidthF(1.5)
            if kind == "rapid":
                pen.setStyle(Qt.PenStyle.DashLine)
            base_pens[kind] = pen
        default_pen = QPen(QColor(80, 80, 80))
        default_pen.setCosmetic(True)
        for seg in segments:
            x1 = float(seg.start.x); y1 = float(seg.start.y)
            x2 = float(seg.end.x);   y2 = float(seg.end.y)
            item = QGraphicsLineItem(x1, y1, x2, y2)
            pen = base_pens.get(seg.kind, default_pen)
            item.setPen(pen)
            self._scene.addItem(item)
            self._original_pens[item] = pen
            self._items_by_line.setdefault(seg.line, []).append(item)

    def _highlight_width(self) -> float:
        return 3.0  # cosmetic pen — device pixels, not scene units

    def _show_overlay(self, text: str) -> None:
        self._scene.clear()
        item = QGraphicsSimpleTextItem(text)
        item.setBrush(QBrush(QColor(200, 60, 60)))
        self._scene.addItem(item)
        self._overlay_item = item
        self._scene.setSceneRect(item.boundingRect().adjusted(-4, -4, 4, 4))
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
