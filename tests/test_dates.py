import sqlite3
from datetime import datetime, timezone

from foto.catalog import LibraryFilter
from foto.catalog.db import MIGRATIONS, migrate
from foto.importer import import_folder


def _shoot(tmp_path, name, times):
    from conftest import make_jpeg

    for i, when in enumerate(times):
        make_jpeg(tmp_path / name / f"{i}.jpg", when=when)
    return tmp_path / name


def test_date_sources(catalog, tmp_path):
    import_folder(catalog, str(_shoot(tmp_path, "a", ["2026:06:08 23:38:10", "2026:06:09 00:20:34",
                                                      "2026:06:09 14:00:00", "2025:12:31 10:00:00"])))
    assert catalog.day_counts() == [("2025-12-31", 1), ("2026-06-08", 1), ("2026-06-09", 2)]

    def n(prefix):
        return len(catalog.query(LibraryFilter(source="date", source_id=prefix)))

    assert (n("2026"), n("2026-06"), n("2026-06-09"), n("2025-12"), n("2026-05")) == (3, 3, 2, 1, 0)


def test_import_batches(catalog, tmp_path):
    first = import_folder(catalog, str(_shoot(tmp_path, "a", ["2026:01:01 10:00:00"] * 2)))
    again = import_folder(catalog, str(tmp_path / "a"))
    second = import_folder(catalog, str(_shoot(tmp_path, "b", ["2026:02:01 10:00:00"] * 3)))
    assert again.import_id is None  # nothing new, no empty batch
    assert [(b.id, b.count) for b in catalog.imports()] == [(second.import_id, 3), (first.import_id, 2)]
    assert catalog.imports()[0].root == str(tmp_path / "b")
    assert len(catalog.query(LibraryFilter(source="import", source_id=first.import_id))) == 2


def test_migration_groups_existing_imports(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    for i, script in enumerate(MIGRATIONS[:2]):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {i + 1};\nCOMMIT;")
    conn.execute("INSERT INTO folders(id, path) VALUES (1, 'x')")
    times = ["2026-01-01T10:00:00", "2026-01-01T10:02:00", "2026-01-01T10:04:30", "2026-01-01T10:30:00"]
    for i, t in enumerate(times):
        conn.execute(
            "INSERT INTO images(folder_id, path, filename, ext, file_size, mtime_ns, imported_at) "
            "VALUES (1, ?, ?, '.jpg', 1, 1, ?)", (f"p{i}", f"p{i}", t))
    conn.commit()
    migrate(conn)
    batches = conn.execute("SELECT import_id FROM images ORDER BY imported_at").fetchall()
    assert [b[0] for b in batches] == [1, 1, 1, 2]
    assert conn.execute("SELECT started_at FROM imports ORDER BY id").fetchall() == [
        ("2026-01-01T10:00:00",), ("2026-01-01T10:30:00",)]


def test_import_label():
    from foto.ui.sidebar import import_label

    now = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc).astimezone()
    assert import_label("2026-09-27T17:59:40", now) == "Just now"
    assert import_label("2026-09-27T17:48:00", now) == "12 minutes ago"
    assert import_label("2026-09-27T16:30:00", now) == "1 hour ago"
    assert import_label("2026-09-27T12:00:00", now) == "6 hours ago"
    old = datetime(2026, 1, 25, 21, 16, tzinfo=timezone.utc).astimezone()
    assert import_label("2026-01-25T21:16:00", now) == f"January 25, {old.hour % 12 or 12}:16 {'AM' if old.hour < 12 else 'PM'}"
    assert import_label("2024-01-25T21:16:00", now).startswith("January 25, 2024")


def test_sidebar_date_tree(qapp, catalog, tmp_path):
    from foto.ui.sidebar import Sidebar

    import_folder(catalog, str(_shoot(tmp_path, "a", ["2026:05:03 10:00:00", "2026:06:08 23:38:10",
                                                      "2026:06:09 00:20:34", "2025:12:31 10:00:00"])))
    bar = Sidebar(catalog)
    years = [bar._dates.child(i) for i in range(bar._dates.childCount())]
    assert [(y.text(0), y.text(1)) for y in years] == [("2026", "3"), ("2025", "1")]
    assert years[0].isExpanded() and not years[1].isExpanded()  # newest year open
    june = years[0].child(1)
    assert (june.text(0), june.text(1)) == ("June", "2")
    assert [june.child(i).text(0) for i in range(2)] == ["Monday, Jun 8", "Tuesday, Jun 9"]

    got = []
    bar.sourceChanged.connect(lambda kind, sid: got.append((kind, sid)))
    bar.setCurrentItem(june.child(1))
    assert got[-1] == ("date", "2026-06-09")

    years[1].setExpanded(True)
    june.setExpanded(False)
    bar.refresh()  # user's expand/collapse survives a refresh, and so does the selection
    assert bar._dates.child(1).isExpanded() and not bar._dates.child(0).child(1).isExpanded()
    assert bar.current_source() == ("date", "2026-06-09")
    assert bar._recent.childCount() == 1
