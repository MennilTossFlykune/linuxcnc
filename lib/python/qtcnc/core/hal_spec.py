"""HAL pin specifications.

Separate from core.types so widget modules can import HalPinSpec without
pulling in the full status-type universe. The daemon uses these specs to
create real pins on its hal.component; the client uses them as declaration
records to send in DECLARE_PINS messages.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any


class HalType(IntEnum):
    BIT = 1
    FLOAT = 2
    S32 = 3
    U32 = 4


class HalDir(IntEnum):
    IN = 1
    OUT = 2
    IO = 3


@dataclass(frozen=True, slots=True)
class HalPinSpec:
    """Declaration for a single HAL pin.

    The name may contain a `{name}` placeholder which is substituted with
    the widget's objectName() at bootstrap time. After substitution the
    name is final and used verbatim when creating the pin on the daemon's
    hal.component.
    """

    name: str
    type: HalType
    dir: HalDir
    initial: Any = None
    owner_widget: str | None = None

    def substitute(self, widget_object_name: str) -> "HalPinSpec":
        """Return a new spec with `{name}` replaced in the pin name.

        Raises KeyError if the template references an unknown key. Returns
        self unchanged if the template contains no placeholders.
        """
        new_name = self.name.format(name=widget_object_name)
        if new_name == self.name:
            return self
        return replace(self, name=new_name, owner_widget=widget_object_name)
