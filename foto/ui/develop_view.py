"""Develop view: a photo rendered from its raw data (the same pane as the loupe, plus what it's showing).

The adjustment panel joins it in the next milestone. Backslash toggles the camera's own JPEG.
"""

from __future__ import annotations

from foto.ui.compare import LoupeView


class DevelopView(LoupeView):
    def __init__(self, service, loader, color, settings_for, parent=None):
        super().__init__(service, loader, color, settings_for, show_source=True, parent=parent)

    def toggle_camera(self) -> None:
        self.pane.toggle_camera()

    def refresh_settings(self) -> None:
        self.pane.refresh_settings()
