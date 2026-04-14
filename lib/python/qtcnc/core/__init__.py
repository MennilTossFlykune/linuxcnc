"""Core types, state, status, command, config, program, devices.

Must not import anything from qtcnc.transport.zmq_* — widgets get loaded by
Qt Designer which would explode if we drag pyzmq into the plugin loader.
"""
