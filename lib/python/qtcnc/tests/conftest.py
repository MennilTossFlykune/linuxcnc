"""Shared pytest fixtures for qtcnc tests."""
import os
import sys

# Make the qtcnc package importable when running pytest from the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PYTHON_DIR = os.path.normpath(os.path.join(_HERE, "..", ".."))
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)
