"""Widget library: QtcncWidget base class, HalPin proxy, and common widgets.

Widgets must import only from qtcnc.core.types, qtcnc.core.hal_spec, and
qtpy. No transport/zmq_* imports — Qt Designer loads widget modules as
plugins and any import failure breaks the plugin loader.
"""
