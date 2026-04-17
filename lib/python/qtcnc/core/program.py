"""Client-side g-code preview parser.

Wraps the `gcode` C extension (built from `src/emc/rs274ngc/gcodemodule.cc`)
with a recorder that collects toolpath segments into frozen dataclasses
and computes axis-aligned extents. Used by `GcodeView` and `GcodePreview`
to render a loaded program without going through the daemon.

Why a fresh wrapper instead of reusing `lib/python/rs274/glcanon.py`:
that module pulls in OpenGL, AXIS color tables, Tk, and a full canon
recorder geared toward live machine-state highlighting. The qtcnc
preview only needs segment lists + extents + line indexing, with no
GUI dependencies — so this file copies just the canon dispatch pattern
from `interpret.Translated` / `glcanon.GLCanon` and emits dataclasses.

Public API:

    program = parse("/path/to/file.ngc")
    program.segments     # tuple[ToolpathSegment, ...]
    program.extents      # ToolpathExtents
    program.line_index   # dict[int, list[int]]: line -> segment indices
    program.line_count   # int — total source lines
"""

from __future__ import annotations

import atexit
import ctypes
import math
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from qtcnc.core.types import Position


# The `gcode` C extension links against `libtooldata.so`, whose
# `tooldata_find_index_for_tool()` dereferences a process-local
# `tool_mmap_base` pointer that is NULL until either `tool_mmap_user()`
# or `tool_mmap_creator()` has been called in the current process.
# Any g-code with a T word hits `convert_tool_select` ->
# `find_tool_index` -> `tooldata_find_index_for_tool` -> segfault.
#
# AXIS gets around this because it imports `linuxcnc`, and
# `linuxcnc.stat().poll()` calls `tool_mmap_user()` on the first poll
# (see src/emc/usr_intf/axis/extensions/emcmodule.cc). qtcnc's client
# deliberately does NOT import `linuxcnc` -- the whole point of the
# daemon split -- so we have to initialise the mmap ourselves.
#
# The mmap must be populated with dummy entries for all 1000 pockets
# so that M6 tool changes succeed during preview parsing. The C
# interpreter checks the mmap via tooldata_find_index_for_tool()
# during M6 processing — if the tool isn't found, M6 fails before
# the change_tool callback fires on the recorder.
_tool_mmap_ready = False

_CANON_POCKETS_MAX = 1001


class _PmCartesian(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_double),
        ("y", ctypes.c_double),
        ("z", ctypes.c_double),
    ]


class _EmcPose(ctypes.Structure):
    _fields_ = [
        ("tran", _PmCartesian),
        ("a", ctypes.c_double),
        ("b", ctypes.c_double),
        ("c", ctypes.c_double),
        ("u", ctypes.c_double),
        ("v", ctypes.c_double),
        ("w", ctypes.c_double),
    ]


class _CanonToolTable(ctypes.Structure):
    _fields_ = [
        ("toolno", ctypes.c_int),
        ("pocketno", ctypes.c_int),
        ("offset", _EmcPose),
        ("diameter", ctypes.c_double),
        ("frontangle", ctypes.c_double),
        ("backangle", ctypes.c_double),
        ("orientation", ctypes.c_int),
        ("comment", ctypes.c_char * 40),
    ]


def _ensure_tool_mmap() -> None:
    """Create a sandbox tool mmap populated with dummy entries.

    Always creates an isolated mmap under a tempdir (never attaches
    to a running LinuxCNC's shared mmap) and fills every pocket 1-1000
    with a dummy tool entry so tooldata_find_index_for_tool() succeeds
    for any tool number during preview parsing.
    """
    global _tool_mmap_ready
    if _tool_mmap_ready:
        return
    try:
        lib = ctypes.CDLL("libtooldata.so.0", mode=ctypes.RTLD_GLOBAL)
    except OSError as e:
        raise GcodeParseError(
            "", -1, -1, f"libtooldata.so.0 not found: {e}"
        ) from e
    lib.tool_mmap_creator.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.tool_mmap_creator.restype = ctypes.c_int
    lib.tooldata_put.argtypes = [_CanonToolTable, ctypes.c_int]
    lib.tooldata_put.restype = ctypes.c_int

    sandbox = tempfile.mkdtemp(prefix="qtcnc-tool-")
    atexit.register(shutil.rmtree, sandbox, ignore_errors=True)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = sandbox
    try:
        rc = lib.tool_mmap_creator(None, 0)
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
    if rc != 0:
        raise GcodeParseError("", int(rc), -1, "tool_mmap_creator failed")

    for idx in range(1, _CANON_POCKETS_MAX):
        entry = _CanonToolTable()
        entry.toolno = idx
        entry.pocketno = idx
        lib.tooldata_put(entry, idx)

    _tool_mmap_ready = True


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolpathSegment:
    """A single straight-line segment of the toolpath.

    Curved moves (`gcode.parse` calls `arc_feed`) are tessellated into
    multiple `ToolpathSegment`s with `kind="arc"` so the renderer can
    treat the whole program uniformly as line strips.
    """

    start: Position
    end: Position
    kind: str  # "rapid" | "feed" | "arc" | "probe" | "rigid_tap"
    line: int
    feedrate: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolpathExtents:
    """Axis-aligned bounding box of the parsed toolpath."""

    min: Position
    max: Position

    @property
    def is_empty(self) -> bool:
        return self.min == self.max == Position()


@dataclass(frozen=True, slots=True)
class GcodeProgram:
    """A parsed g-code program: segments + extents + line index.

    `line_index[n]` gives the indices into `segments` for the segments
    emitted on source line `n`. Empty list for lines that emitted no
    motion (comments, m-codes, dwells, etc.).

    `requested_tools` is the set of tool numbers the program's M6
    tool changes referenced during parsing. Callers can diff this
    against the tool database to detect missing tools.
    """

    path: str
    segments: tuple[ToolpathSegment, ...]
    extents: ToolpathExtents
    line_index: dict[int, list[int]] = field(default_factory=dict)
    line_count: int = 0
    units: str = "mm"
    requested_tools: frozenset[int] = field(default_factory=frozenset)

    def segments_on_line(self, line: int) -> list[ToolpathSegment]:
        return [self.segments[i] for i in self.line_index.get(line, ())]


# ---------------------------------------------------------------------------
# Internal recorder — implements the canon callback contract
# ---------------------------------------------------------------------------


def _make_position(x: float, y: float, z: float,
                   a: float, b: float, c: float,
                   u: float, v: float, w: float) -> Position:
    """Build a Position with the rotary/uvw axes left as None when zero.

    The canon callback always supplies all 9 floats; we collapse 0.0
    rotary/uvw values to None so output Positions are interchangeable
    with `Status.position` for renderer code that only cares about XYZ.
    """
    return Position(
        x=x, y=y, z=z,
        a=a if a else None,
        b=b if b else None,
        c=c if c else None,
        u=u if u else None,
        v=v if v else None,
        w=w if w else None,
    )


# Empty-spindle data shape matches what the C interp expects from
# `get_tool`: (id, x, y, z, a, b, c, u, v, w, diameter, frontangle, backangle, orientation).
_EMPTY_TOOL = (-1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)


class _CanonRecorder:
    """Receives canon callbacks from `gcode.parse` and accumulates segments.

    The C `gcode` module dispatches to attribute methods on this object
    via `PyObject_CallMethod`. We implement the slow Python path only
    (no PyCapsule fast callbacks) because parse-once preview parsing
    isn't on the hot path — accuracy matters more than micro-perf, and
    the slow path makes debugging straightforward.

    Follows the attribute contract of `rs274.interpret.Translated`:
    `lo` is a 9-tuple in canonical (inches) machine coordinates, and
    the G92/G5x offsets are exposed as 18 flat `g*_offset_<axis>`
    attributes. `gcode.arc_to_segments` reads those names directly via
    `PyObject_GetAttrString`, so their spelling matters.

    Unit conversion: the canon ALWAYS delivers values in inches
    (per `GET_EXTERNAL_LENGTH_UNIT_TYPE() == CANON_UNITS_INCHES`). We
    store everything in inches internally to keep the math consistent
    with `arc_to_segments`, then scale to the caller's requested units
    only when building the final `ToolpathSegment` dataclasses — same
    pattern as `glcanon.from_internal_units`.
    """

    # The C interp accesses `parameter_file` as a plain attribute (not a
    # method) via PyObject_GetAttrString. If it's missing, the Python
    # AttributeError is left set and the very next callmethod returns
    # NULL. Empty string causes a link() segfault inside Interp::init().
    # `parse()` overrides this with a real tempfile path before calling
    # gcode.parse().
    parameter_file: str = ""

    # Default offset + plane values, per `Translated` / `ArcsToSegmentsMixin`.
    # Defined at class scope so `gcode.arc_to_segments` sees valid values
    # even before any set_g5x_offset / set_g92_offset / set_plane callback.
    g92_offset_x = g92_offset_y = g92_offset_z = 0.0
    g92_offset_a = g92_offset_b = g92_offset_c = 0.0
    g92_offset_u = g92_offset_v = g92_offset_w = 0.0
    g5x_offset_x = g5x_offset_y = g5x_offset_z = 0.0
    g5x_offset_a = g5x_offset_b = g5x_offset_c = 0.0
    g5x_offset_u = g5x_offset_v = g5x_offset_w = 0.0
    rotation_xy = 0.0
    rotation_cos = 1.0
    rotation_sin = 0.0
    plane = 1
    arcdivision = 64

    def __init__(
        self,
        units: str = "mm",
        tool_table: dict[int, tuple] | None = None,
    ) -> None:
        self._segments: list[ToolpathSegment] = []
        self._line_index: dict[int, list[int]] = {}
        # `lo` is the last machine-coordinate position in canonical (inch)
        # units, as a 9-tuple. `gcode.arc_to_segments` reads it via
        # `get_attr(canon, "lo", "ddddddddd:...")`, so it must be public
        # and tuple-shaped.
        self.lo: tuple[float, float, float, float, float, float, float, float, float] = (0.0,) * 9
        self._lineno = -1
        self._feedrate = 0.0
        self._suppress = 0
        self._units = units
        # Scale applied at segment-emit time: 25.4 for mm, 1.0 for inch.
        # Rotary axes (a/b/c) stay in degrees — only xyz/uvw are scaled.
        self._unit_scale = 25.4 if units == "mm" else 1.0
        self._g5x_index = 1
        # Tool-length offset (currently unused for motion, but tracked
        # so tool_offset() can update the stored `lo` consistently).
        self._tool_offset = (0.0,) * 9
        # Rolling axis-aligned bounding box in DISPLAY units (already scaled).
        self._min = [math.inf] * 9
        self._max = [-math.inf] * 9
        self._tool_table = tool_table or {}
        self._requested_tools: set[int] = set()

    # ----- public accessors -----

    @property
    def segments(self) -> list[ToolpathSegment]:
        return self._segments

    @property
    def line_index(self) -> dict[int, list[int]]:
        return self._line_index

    @property
    def requested_tools(self) -> frozenset[int]:
        return frozenset(self._requested_tools)

    def extents(self) -> ToolpathExtents:
        if not self._segments:
            zero = Position()
            return ToolpathExtents(min=zero, max=zero)
        return ToolpathExtents(
            min=_make_position(*self._min),
            max=_make_position(*self._max),
        )

    # ----- coordinate helpers -----

    def rotate_and_translate(
        self, x: float, y: float, z: float,
        a: float, b: float, c: float,
        u: float, v: float, w: float,
    ) -> tuple[float, float, float, float, float, float, float, float, float]:
        """Apply G92 → XY rotation → G5x offsets, in canonical inches.

        Mirrors `rs274.interpret.Translated.rotate_and_translate` exactly.
        The result lives in machine coordinates (still inches) — callers
        feed it into `self.lo` and into `_record()`, which handles the
        inches→display-units scaling at emit time.
        """
        x += self.g92_offset_x
        y += self.g92_offset_y
        z += self.g92_offset_z
        a += self.g92_offset_a
        b += self.g92_offset_b
        c += self.g92_offset_c
        u += self.g92_offset_u
        v += self.g92_offset_v
        w += self.g92_offset_w
        if self.rotation_xy:
            rx = x * self.rotation_cos - y * self.rotation_sin
            y = x * self.rotation_sin + y * self.rotation_cos
            x = rx
        x += self.g5x_offset_x
        y += self.g5x_offset_y
        z += self.g5x_offset_z
        a += self.g5x_offset_a
        b += self.g5x_offset_b
        c += self.g5x_offset_c
        u += self.g5x_offset_u
        v += self.g5x_offset_v
        w += self.g5x_offset_w
        return (x, y, z, a, b, c, u, v, w)

    def _scale(
        self, pos: tuple[float, float, float, float, float, float, float, float, float],
    ) -> tuple[float, float, float, float, float, float, float, float, float]:
        s = self._unit_scale
        return (pos[0]*s, pos[1]*s, pos[2]*s,
                pos[3], pos[4], pos[5],
                pos[6]*s, pos[7]*s, pos[8]*s)

    def _grow_extents(
        self, pos: tuple[float, float, float, float, float, float, float, float, float],
    ) -> None:
        for i, v in enumerate(pos):
            if v < self._min[i]:
                self._min[i] = v
            if v > self._max[i]:
                self._max[i] = v

    def _record(
        self,
        kind: str,
        end: tuple[float, float, float, float, float, float, float, float, float],
    ) -> None:
        """Append a segment from self.lo → end.

        `self.lo` and `end` are both in canonical inches; we scale to
        display units at emit time so the dataclass positions match the
        caller's `units=...` request.
        """
        # Skip zero-length moves: the canon emits an initial position
        # set as a traverse to (0,0,0) which would render as a stray
        # zero-length segment otherwise. After that, every real move
        # emits a segment.
        if end == self.lo:
            self.lo = end
            return
        start_scaled = self._scale(self.lo)
        end_scaled = self._scale(end)
        seg = ToolpathSegment(
            start=_make_position(*start_scaled),
            end=_make_position(*end_scaled),
            kind=kind,
            line=self._lineno,
            feedrate=self._feedrate,
        )
        idx = len(self._segments)
        self._segments.append(seg)
        self._line_index.setdefault(self._lineno, []).append(idx)
        self._grow_extents(start_scaled)
        self._grow_extents(end_scaled)
        self.lo = end

    # ----- canon callbacks the C interpreter invokes -----

    def next_line(self, state) -> None:
        self._lineno = getattr(state, "sequence_number", -1)

    def comment(self, _text: str) -> None:
        pass

    def message(self, _text: str) -> None:
        pass

    def check_abort(self) -> int:
        return 0

    def set_feed_rate(self, rate: float) -> None:
        # rate arrives as units/min; segment renderer treats it as opaque.
        self._feedrate = rate / 60.0

    def set_traverse_rate(self, _rate: float) -> None:
        pass

    def set_feed_mode(self, _mode: int) -> None:
        pass

    def set_spindle_rate(self, _rate: float) -> None:
        pass

    def set_plane(self, plane: int) -> None:
        self.plane = plane

    def set_xy_rotation(self, theta: float) -> None:
        self.rotation_xy = theta
        t = math.radians(theta)
        self.rotation_sin = math.sin(t)
        self.rotation_cos = math.cos(t)

    def set_g5x_offset(
        self, index: int,
        x: float, y: float, z: float,
        a: float, b: float, c: float,
        u: float = 0.0, v: float = 0.0, w: float = 0.0,
    ) -> None:
        self._g5x_index = index
        self.g5x_offset_x = x
        self.g5x_offset_y = y
        self.g5x_offset_z = z
        self.g5x_offset_a = a
        self.g5x_offset_b = b
        self.g5x_offset_c = c
        self.g5x_offset_u = u
        self.g5x_offset_v = v
        self.g5x_offset_w = w

    def set_g92_offset(
        self,
        x: float, y: float, z: float,
        a: float, b: float, c: float,
        u: float = 0.0, v: float = 0.0, w: float = 0.0,
    ) -> None:
        self.g92_offset_x = x
        self.g92_offset_y = y
        self.g92_offset_z = z
        self.g92_offset_a = a
        self.g92_offset_b = b
        self.g92_offset_c = c
        self.g92_offset_u = u
        self.g92_offset_v = v
        self.g92_offset_w = w

    def tool_offset(
        self, xo: float, yo: float, zo: float,
        ao: float, bo: float, co: float,
        uo: float, vo: float, wo: float,
    ) -> None:
        self._tool_offset = (xo, yo, zo, ao, bo, co, uo, vo, wo)

    def change_tool(self, tool_nr: int) -> None:
        if tool_nr > 0:
            self._requested_tools.add(tool_nr)

    def get_tool(self, pocket: int):
        if pocket in self._tool_table:
            return self._tool_table[pocket]
        if pocket > 0:
            return (pocket, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
        return _EMPTY_TOOL

    def get_block_delete(self) -> int:
        return 0

    def get_axis_mask(self) -> int:
        # XYZABC = 0x3F = 63. The interpreter uses this to decide which
        # axes are valid in the canonical frame; XYZABC covers the
        # common 6-axis case and works for 3-axis files too.
        return 63

    def get_external_angular_units(self) -> float:
        return 1.0

    def get_external_length_units(self) -> float:
        # Reports the host's length-unit factor relative to inches:
        # 1.0 means "host wants inches", 25.4 would mean "host wants
        # mm". The interpreter uses this to interpret values coming
        # FROM the host (e.g., tool table entries) — it does NOT
        # rescale the canonical positions delivered to the canon
        # callbacks, which are always in inches. We do the inches→mm
        # conversion ourselves in `_scale` at segment-emit time.
        return 1.0

    def user_defined_function(self, _i: int, _p: float, _q: float) -> None:
        pass

    def dwell(self, _seconds: float) -> None:
        pass

    # ----- motion callbacks -----

    def straight_traverse(
        self, x: float, y: float, z: float,
        a: float, b: float, c: float,
        u: float, v: float, w: float,
    ) -> None:
        if self._suppress > 0:
            return
        end = self.rotate_and_translate(x, y, z, a, b, c, u, v, w)
        self._record("rapid", end)

    def straight_feed(
        self, x: float, y: float, z: float,
        a: float, b: float, c: float,
        u: float, v: float, w: float,
    ) -> None:
        if self._suppress > 0:
            return
        end = self.rotate_and_translate(x, y, z, a, b, c, u, v, w)
        self._record("feed", end)

    def straight_probe(
        self, x: float, y: float, z: float,
        a: float, b: float, c: float,
        u: float, v: float, w: float,
    ) -> None:
        if self._suppress > 0:
            return
        end = self.rotate_and_translate(x, y, z, a, b, c, u, v, w)
        self._record("probe", end)

    def rigid_tap(self, x: float, y: float, z: float) -> None:
        if self._suppress > 0:
            return
        # Rigid tap moves down to (x,y,z) at feed, then back to where it
        # started. Emit two segments so the renderer shows the full move.
        end = self.rotate_and_translate(
            x, y, z,
            self.lo[3], self.lo[4], self.lo[5],
            self.lo[6], self.lo[7], self.lo[8],
        )
        prior = self.lo
        self._record("rigid_tap", end)
        self._record("rigid_tap", prior)

    def arc_feed(
        self, x1: float, y1: float, cx: float, cy: float, rot: int,
        z1: float,
        a: float, b: float, c: float,
        u: float, v: float, w: float,
    ) -> None:
        if self._suppress > 0:
            return
        # Defer to the C helper that tessellates an arc into N segments.
        # Importing here keeps `gcode` out of module-import time so
        # widgets/Designer that touch this file don't have to drag in
        # the C extension just to see the dataclasses.
        import gcode  # type: ignore[import-not-found]
        try:
            segs = gcode.arc_to_segments(
                self, x1, y1, cx, cy, rot, z1, a, b, c, u, v, w,
                self.arcdivision,
            )
        except Exception:
            # If the helper is unavailable (e.g., unit test environment),
            # fall back to a single straight chord — better than dropping
            # the move entirely.
            end = self.rotate_and_translate(x1, y1, z1, a, b, c, u, v, w)
            self._record("arc", end)
            return
        # arc_to_segments returns machine-coordinate tuples already —
        # don't rotate-and-translate them a second time.
        for s in segs:
            self._record("arc", tuple(s))

    def straight_arcsegments(self, segs) -> None:
        # Some canon recorders take pre-tessellated arc segments via
        # this entry point. We don't normally use it (arc_feed handles
        # tessellation inline), but expose it for completeness.
        for s in segs:
            self._record("arc", tuple(s))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


_PARSE_ERROR_THRESHOLD = 3  # gcode.MIN_ERROR at the time of writing


class GcodeParseError(RuntimeError):
    """Raised when `gcode.parse` returns an error code or fails outright."""

    def __init__(self, path: str, code: int, line: int, message: str = "") -> None:
        super().__init__(
            f"failed to parse {path!r}: code={code} line={line} {message}".rstrip()
        )
        self.path = path
        self.code = code
        self.line = line
        self.message = message


def parse(
    path: str,
    *,
    units: str = "mm",
    initcode: Optional[str] = None,
    tool_table: dict[int, tuple] | None = None,
    lenient: bool = False,
) -> GcodeProgram:
    """Parse a g-code file and return a `GcodeProgram`.

    When `tool_table` is provided, the preview uses real tool geometry
    (correct diameters, Z offsets). Missing tools get zero offsets.
    The returned `GcodeProgram.requested_tools` records every pocket
    the program referenced during parsing.

    When `lenient` is True, interpreter errors (e.g. tool changes for
    tools missing from the mmap) produce a partial result instead of
    raising GcodeParseError. Callers that only need `requested_tools`
    should pass `lenient=True`.

    Imports the `gcode` C extension lazily so this module can be
    imported (e.g. by Designer registering widgets) on hosts that
    don't have the LinuxCNC C extensions built.
    """
    import gcode  # type: ignore[import-not-found]

    _ensure_tool_mmap()
    recorder = _CanonRecorder(units=units, tool_table=tool_table)
    # The C interp's init() opens recorder.parameter_file as a writable
    # variable file (rs274ngc.var-style). An empty string segfaults — it
    # tries link()/rename() against an empty path and the failure is not
    # handled cleanly. Mirror AXIS (axis.py:1251-1255): give it a real,
    # writable temp file. We don't care about the variables it persists
    # for preview parsing, so it's discarded after parse().
    tmp_var = tempfile.NamedTemporaryFile(
        mode="w", prefix="qtcnc-vars-", suffix=".var", delete=False,
    )
    tmp_var.close()
    recorder.parameter_file = tmp_var.name
    # gcode.parse signature: (filename, callback[, unitcode, initcode, interpname]).
    # We pass `initcode` (a single line of g-code prepended to the file)
    # to force the interpreter into millimeter-or-inch mode regardless
    # of the file's own units header. Default: G21 (mm).
    unit_init = initcode or ("G21" if units == "mm" else "G20")
    try:
        result, last_seq = gcode.parse(path, recorder, "", unit_init)
    except Exception as e:
        if not lenient:
            raise GcodeParseError(path, -1, -1, str(e)) from e
        result, last_seq = -1, -1
    finally:
        try:
            os.unlink(tmp_var.name)
        except OSError:
            pass
    if result > _PARSE_ERROR_THRESHOLD and not lenient:
        raise GcodeParseError(path, int(result), int(last_seq))

    # Count source lines for the line_count field. Cheap; the file is
    # already on disk in the OS page cache after parsing.
    line_count = 0
    try:
        with open(path, "rb") as f:
            for line_count, _ in enumerate(f, 1):
                pass
    except OSError:
        pass

    return GcodeProgram(
        path=path,
        segments=tuple(recorder.segments),
        extents=recorder.extents(),
        line_index=dict(recorder.line_index),
        line_count=line_count,
        units=units,
        requested_tools=recorder.requested_tools,
    )
