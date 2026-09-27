"""Entry point: `foto` or `python -m foto`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from foto.config import CatalogPaths, default_catalog_dir
from foto.ui.image_view import set_default_gl_format


def dark_palette() -> QPalette:
    p = QPalette()
    base, window, text = QColor(34, 34, 34), QColor(46, 46, 46), QColor(220, 220, 220)
    for role, color in (
        (QPalette.Window, window), (QPalette.WindowText, text), (QPalette.Base, base),
        (QPalette.AlternateBase, window), (QPalette.Text, text), (QPalette.Button, window),
        (QPalette.ButtonText, text), (QPalette.ToolTipBase, window), (QPalette.ToolTipText, text),
        (QPalette.Highlight, QColor(90, 90, 90)), (QPalette.HighlightedText, QColor(255, 255, 255)),
        (QPalette.PlaceholderText, QColor(120, 120, 120)), (QPalette.Link, QColor(120, 170, 230)),
    ):
        p.setColor(role, color)
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(115, 115, 115))
    return p


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="foto", description="Personal photo library")
    ap.add_argument("--catalog", type=Path, help=f"catalog folder (default: {default_catalog_dir()})")
    ap.add_argument("--import", dest="import_folder", help="import this folder on start")
    args = ap.parse_args(argv)
    # cmd.exe and older PowerShell pass "~" through literally; expand it ourselves.
    if args.catalog:
        args.catalog = args.catalog.expanduser()
    if args.import_folder:
        args.import_folder = str(Path(args.import_folder).expanduser())
    return args


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    set_default_gl_format()
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("Foto")
    app.setApplicationName("Foto")
    app.setStyle("Fusion")
    app.setPalette(dark_palette())

    from foto.ui.main_window import MainWindow  # after QApplication exists

    paths = CatalogPaths(args.catalog or default_catalog_dir()).ensure()
    win = MainWindow(paths)
    win.show()
    if args.import_folder:
        win.start_import(args.import_folder)
    return app.exec()
