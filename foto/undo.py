"""Undo/redo for catalog commands (ratings, flags, edits, tags, collections).

Each step stores catalog snapshots of the affected images from before and
after the command; undo restores "before", redo restores "after".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from foto.catalog import Catalog

MAX_STEPS = 200


@dataclass
class Step:
    label: str
    before: dict[int, dict]
    after: dict[int, dict]


class UndoStack:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._undo: list[Step] = []
        self._redo: list[Step] = []

    def run(self, label: str, ids: Sequence[int], command: Callable[[], None]) -> None:
        """Run a command that changes these images, recording it as one undoable step."""
        before = self.catalog.snapshot(ids)
        command()
        after = self.catalog.snapshot(ids)
        if after != before:
            self._undo.append(Step(label, before, after))
            del self._undo[:-MAX_STEPS]
            self._redo.clear()

    def undo_label(self) -> str | None:
        return self._undo[-1].label if self._undo else None

    def redo_label(self) -> str | None:
        return self._redo[-1].label if self._redo else None

    def undo(self) -> Step | None:
        if not self._undo:
            return None
        step = self._undo.pop()
        self.catalog.restore(step.before, f"Undo {step.label.lower()}")
        self._redo.append(step)
        return step

    def redo(self) -> Step | None:
        if not self._redo:
            return None
        step = self._redo.pop()
        self.catalog.restore(step.after, f"Redo {step.label.lower()}")
        self._undo.append(step)
        return step

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
