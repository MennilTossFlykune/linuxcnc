"""Qt Designer integration for qtcnc widgets.

Files in this directory are loaded by Qt Designer when ``PYQTDESIGNERPATH``
points here. The ``qtcnc-designer`` launcher (``bin/qtcnc-designer``) sets
that variable plus ``PYTHONPATH`` so Designer's embedded Python can resolve
``qtcnc.widgets.*`` imports.

Designer scans every ``.py`` file in the path for subclasses of
``QPyDesignerCustomWidgetPlugin``. The actual plugin classes live in
``qtcnc_widget_plugins.py``; the base class ``_QtcncDesignerPlugin`` lives
in ``_plugin_base.py`` and is harmless to import inside Designer.
"""
