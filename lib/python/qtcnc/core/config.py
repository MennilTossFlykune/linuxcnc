"""QtcncConfig: client-side configuration dataclass.

The daemon reads the INI file directly via the existing `iniinfo` module
and delivers state over the wire. The client only needs a small amount
of cosmetic information (axis count, work coord count, window title) so
it can size its widgets correctly. That cosmetic set lives here.

In mock mode without an INI file, `QtcncConfig.mock_default()` returns
plausible values and never touches the filesystem.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class QtcncConfig:
    """Client-side cosmetic configuration."""

    window_title: str = "qtcnc"
    axis_count: int = 3
    axis_letters: str = "XYZ"
    work_coords_count: int = 9
    ini_path: Optional[str] = None

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
        return cls(
            window_title=title,
            axis_count=len(letters),
            axis_letters=letters,
            work_coords_count=9,
            ini_path=path,
        )
