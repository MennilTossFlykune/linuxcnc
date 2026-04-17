"""QtcncConfig: client-side configuration dataclass.

Carries the cosmetic and layout-shaping values the client needs to
size its widgets: axis count, work coord count, window title, override
slider ranges. Parsed from an INI file with stdlib `configparser`.

`QtcncConfig.mock_default()` returns plausible values for mock mode
and never touches the filesystem.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class QtcncConfig:
    """Client-side cosmetic configuration."""

    window_title: str = "qtcnc"
    axis_count: int = 3
    axis_letters: str = "XYZ"
    work_coords_count: int = 9
    max_feed_override: float = 1.2
    max_rapid_override: float = 1.0
    max_spindle_override: float = 1.0
    ini_path: Optional[str] = None
    tool_db_path: Optional[str] = None
    random_toolchanger: bool = False

    @classmethod
    def mock_default(cls) -> "QtcncConfig":
        return cls(window_title="qtcnc (mock)")

    @classmethod
    def from_ini(cls, path: str) -> "QtcncConfig":
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read(path)
        title = cp.get("DISPLAY", "TITLE", fallback="qtcnc")
        coords = cp.get("TRAJ", "COORDINATES", fallback="XYZ")
        letters = "".join(c for c in coords if c.isalpha()).upper() or "XYZ"
        ini_dir = os.path.dirname(os.path.abspath(path))
        tool_db_raw = cp.get("QTCNC", "TOOL_DB", fallback=None)
        tool_db_path: Optional[str] = None
        if tool_db_raw is not None:
            if os.path.isabs(tool_db_raw):
                tool_db_path = tool_db_raw
            else:
                tool_db_path = os.path.join(ini_dir, tool_db_raw)
        random_tc = _get_int(cp, "EMCIO", "RANDOM_TOOLCHANGER", 0) != 0
        return cls(
            window_title=title,
            axis_count=len(letters),
            axis_letters=letters,
            work_coords_count=9,
            max_feed_override=_get_float(cp, "DISPLAY", "MAX_FEED_OVERRIDE", 1.2),
            max_rapid_override=_get_float(cp, "DISPLAY", "MAX_RAPID_OVERRIDE", 1.0),
            max_spindle_override=_get_float(cp, "DISPLAY", "MAX_SPINDLE_OVERRIDE", 1.0),
            ini_path=path,
            tool_db_path=tool_db_path,
            random_toolchanger=random_tc,
        )


def _get_float(
    cp: configparser.ConfigParser, section: str, option: str, fallback: float,
) -> float:
    try:
        return float(cp.get(section, option))
    except (configparser.Error, ValueError):
        return fallback


def _get_int(
    cp: configparser.ConfigParser, section: str, option: str, fallback: int,
) -> int:
    try:
        return int(cp.get(section, option))
    except (configparser.Error, ValueError):
        return fallback
