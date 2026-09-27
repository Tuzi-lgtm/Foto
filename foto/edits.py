"""Non-destructive edit operations.

An image's edit settings are a JSON dict in the catalog. Every change writes
a new history entry, so any earlier state can be restored. Originals and
cached previews are never modified; edits are applied at display time.
"""

from __future__ import annotations

from typing import Sequence

from foto.catalog import Catalog


def rotate(catalog: Catalog, ids: Sequence[int], degrees: int) -> None:
    """Rotate by a multiple of 90 degrees (positive = clockwise)."""
    for image_id in ids:
        settings = catalog.get_edit(image_id)
        settings["rotate"] = (int(settings.get("rotate", 0)) + degrees) % 360
        label = "Rotate right" if degrees > 0 else "Rotate left"
        catalog.set_edit(image_id, settings, label)
