"""qtcnc — a fresh GUI framework for LinuxCNC built on qtpy.

Parallel to, not a replacement for, the existing qtvcp framework.

Architecture: a qtcnc-serverd daemon owns linuxcnc.stat/command/error_channel
and hal.component('qtcnc'). The GUI client is a separate process that talks
to the daemon over ZMQ and can run locally or on another machine. A mock
transport backs --mock mode for UI development without linuxcnc running.
"""

__version__ = "0.1.0"
PROTOCOL_VERSION = (1, 1)
