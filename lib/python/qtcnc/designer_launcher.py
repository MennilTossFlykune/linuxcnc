"""Launch Qt Designer with the qtcnc custom-widget plugins registered.

Sets ``PYQTDESIGNERPATH`` to the in-tree ``qtcnc/designer/`` directory
so Designer discovers ``qtcnc_widget_plugins.py``, prepends the in-tree
``lib/python`` to ``PYTHONPATH`` so the plugin file can import
``qtcnc.widgets.*``, and execs Designer.

Binding selection: prefer Qt6 / PyQt6 when both ``PyQt6.QtDesigner`` is
importable and a Qt6 ``designer`` binary is present. Otherwise fall
back to Qt5 / PyQt5. The selected binding is exported as ``QT_API`` so
``qtpy``-based widget code inside the Designer process matches what
this launcher decided.

Usage::

    qtcnc-designer                              # opens an empty Designer
    qtcnc-designer path/to/main.ui              # opens a specific UI file
    qtcnc-designer --screen standard            # opens share/qtcnc/screens/<name>/main.ui
    qtcnc-designer --list-screens
    qtcnc-designer --qt-api pyqt5 --screen minimal
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from importlib import util as _import_util
from pathlib import Path


_HERE = Path(__file__).resolve().parent
_LIB_PYTHON = _HERE.parent
_REPO_ROOT = _LIB_PYTHON.parent.parent
_DESIGNER_DIR = _HERE / "designer"

_BUILTIN_SCREEN_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "share" / "qtcnc" / "screens",
    Path.home() / "linuxcnc" / "qtcnc" / "screens",
)

_log = logging.getLogger("qtcnc.designer")


def _eprint(*args: object) -> None:
    _log.info(" ".join(str(a) for a in args))


def _qt6_designer_binary() -> str | None:
    candidates = (
        "/usr/lib/qt6/bin/designer",
        "/usr/lib/x86_64-linux-gnu/qt6/bin/designer",
    )
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    found = shutil.which("designer-qt6")
    if found:
        return found
    return None


def _qt5_designer_binary() -> str | None:
    candidates = (
        "/usr/lib/qt5/bin/designer",
        "/usr/lib/x86_64-linux-gnu/qt5/bin/designer",
    )
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    found = shutil.which("designer-qt5") or shutil.which("designer")
    if found:
        return found
    return None


def _module_importable(name: str) -> bool:
    try:
        return _import_util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _select_binding(forced: str | None) -> tuple[str, str]:
    """Return ``(qt_api, designer_binary)`` for the chosen binding."""
    candidates: list[tuple[str, str | None, str]] = []
    if forced == "pyqt6":
        candidates = [("pyqt6", _qt6_designer_binary(), "PyQt6.QtDesigner")]
    elif forced == "pyqt5":
        candidates = [("pyqt5", _qt5_designer_binary(), "PyQt5.QtDesigner")]
    else:
        candidates = [
            ("pyqt6", _qt6_designer_binary(), "PyQt6.QtDesigner"),
            ("pyqt5", _qt5_designer_binary(), "PyQt5.QtDesigner"),
        ]
    rejected: list[str] = []
    for api, binary, designer_module in candidates:
        if binary is None:
            rejected.append(f"{api}: no Qt {api[-1]} `designer` binary on PATH")
            continue
        if not _module_importable(designer_module):
            rejected.append(
                f"{api}: {designer_module} not importable "
                f"(install python3-{api}.qtdesigner)"
            )
            continue
        return api, binary
    msg = ["No usable Qt Designer binding found:"]
    for line in rejected:
        msg.append(f"  - {line}")
    raise RuntimeError("\n".join(msg))


def _list_screens() -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for root in _BUILTIN_SCREEN_DIRS:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / "main.ui").is_file():
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                found.append(entry)
    extra = os.environ.get("QTCNC_SCREEN_PATH", "")
    for chunk in extra.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        p = Path(chunk)
        if p.is_dir() and (p / "main.ui").is_file():
            if p.name not in seen:
                seen.add(p.name)
                found.append(p)
    return found


def _resolve_screen(name: str) -> Path:
    for root in _BUILTIN_SCREEN_DIRS:
        candidate = root / name / "main.ui"
        if candidate.is_file():
            return candidate
    extra = os.environ.get("QTCNC_SCREEN_PATH", "")
    for chunk in extra.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        candidate = Path(chunk) / name / "main.ui"
        if candidate.is_file():
            return candidate
    searched = "\n  ".join(str(r / name / "main.ui") for r in _BUILTIN_SCREEN_DIRS)
    raise FileNotFoundError(f"screen {name!r} not found. Searched:\n  {searched}")


def _resolve_target(positional: str | None, screen: str | None) -> Path | None:
    if screen:
        return _resolve_screen(screen)
    if positional is None:
        return None
    p = Path(positional)
    if p.is_file():
        return p.resolve()
    return _resolve_screen(positional)


def _build_env(qt_api: str) -> dict[str, str]:
    env = os.environ.copy()
    env["DESIGNER"] = "1"
    env["QT_API"] = qt_api
    if qt_api == "pyqt5":
        env["QT_SELECT"] = "qt5"
    elif qt_api == "pyqt6":
        env["QT_SELECT"] = "qt6"
    existing_designer_path = env.get("PYQTDESIGNERPATH", "")
    designer_paths = [str(_DESIGNER_DIR)]
    if existing_designer_path:
        designer_paths.append(existing_designer_path)
    env["PYQTDESIGNERPATH"] = os.pathsep.join(designer_paths)
    existing_pythonpath = env.get("PYTHONPATH", "")
    py_paths = [str(_LIB_PYTHON)]
    if existing_pythonpath:
        py_paths.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(py_paths)
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qtcnc-designer",
        description="Launch Qt Designer with the qtcnc custom widgets registered.",
    )
    parser.add_argument(
        "target", nargs="?",
        help="Path to a .ui file, or a screen name from share/qtcnc/screens/.",
    )
    parser.add_argument(
        "--screen", metavar="NAME",
        help="Open share/qtcnc/screens/<NAME>/main.ui (alternative to positional).",
    )
    parser.add_argument(
        "--list-screens", action="store_true",
        help="List available built-in screens and exit.",
    )
    parser.add_argument(
        "--qt-api", choices=("pyqt5", "pyqt6"), default=None,
        help="Force Qt binding. Default: auto-detect (prefer pyqt6).",
    )
    parser.add_argument(
        "--print-env", action="store_true",
        help="Print the environment that would be used and exit (debugging).",
    )
    parser.add_argument(
        "designer_args", nargs=argparse.REMAINDER,
        help="Additional arguments passed through to designer (after `--`).",
    )
    opts = parser.parse_args(argv)

    if opts.list_screens:
        screens = _list_screens()
        if not screens:
            _eprint("(no built-in screens found)")
            return 1
        for screen in screens:
            print(f"{screen.name}\t{screen}")
        return 0

    try:
        qt_api, designer_binary = _select_binding(opts.qt_api)
    except RuntimeError as exc:
        _eprint(str(exc))
        return 2

    try:
        target = _resolve_target(opts.target, opts.screen)
    except FileNotFoundError as exc:
        _eprint(f"qtcnc-designer: {exc}")
        return 2

    env = _build_env(qt_api)

    cmd: list[str] = [designer_binary]
    if target is not None:
        cmd.append(str(target))
    extras = list(opts.designer_args or [])
    if extras and extras[0] == "--":
        extras = extras[1:]
    cmd.extend(extras)

    _eprint(
        f"qtcnc-designer: binding={qt_api} designer={designer_binary} "
        f"target={target if target else '(none)'}"
    )
    if opts.print_env:
        for key in ("PYQTDESIGNERPATH", "PYTHONPATH", "QT_API", "QT_SELECT", "DESIGNER"):
            _eprint(f"  {key}={env.get(key, '')}")
        _eprint(f"  cmd={cmd}")
        return 0

    try:
        proc = subprocess.run(cmd, env=env, check=False)
    except FileNotFoundError:
        _eprint(f"qtcnc-designer: cannot exec {designer_binary!r}")
        return 2
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
