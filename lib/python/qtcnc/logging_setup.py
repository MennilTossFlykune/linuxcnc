"""Logging configuration for the qtcnc framework.

Call `setup_logging()` once at process startup — bootstrap.main() and
server.main() both do this. The hierarchy under "qtcnc" mirrors the
package structure so granular levels work:

    qtcnc.bootstrap, qtcnc.server, qtcnc.transport, qtcnc.designer
"""

from __future__ import annotations

import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger("qtcnc")
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(handler)
