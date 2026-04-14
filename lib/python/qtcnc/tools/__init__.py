"""Operator-facing helper tools that live alongside the qtcnc package.

These don't ship business logic — they just expose `main(argv)` entry
points that can be wrapped by thin `bin/qtcnc-*` shims (or invoked via
`python -m qtcnc.tools.<name>`). Today: only `gen_keys`.
"""
