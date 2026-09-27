from foto import edits
from foto.catalog import LibraryFilter
from foto.catalog.catalog import FLAG_PICK, FLAG_REJECT
from foto.catalog.db import SCHEMA_VERSION
from foto.importer import import_folder


def test_schema_version(catalog):
    assert catalog.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_import_and_reimport(catalog, photo_tree):
    r = import_folder(catalog, str(photo_tree))
    assert (r.added, r.skipped) == (3, 0)
    r = import_folder(catalog, str(photo_tree))
    assert (r.added, r.skipped) == (0, 3)
    names = [x.filename for x in catalog.query()]
    assert names == ["b.jpg", "a.jpg", "c.jpg"]  # capture-time order


def test_metadata(catalog, photo_tree):
    import_folder(catalog, str(photo_tree))
    rec = next(r for r in catalog.query() if r.filename == "c.jpg")
    assert rec.model == "Other Z9"
    assert rec.make == "Foto"
    assert rec.iso == 400
    assert rec.capture_time == "2024-06-01T12:00:00"


def test_ratings_flags_filters(catalog, photo_tree):
    import_folder(catalog, str(photo_tree))
    a, b, c = sorted(catalog.query(), key=lambda r: r.filename)
    catalog.set_rating([a.id, b.id], 4)
    catalog.set_rating([c.id], 9)  # clamped
    catalog.set_flag([a.id], FLAG_PICK)
    catalog.set_flag([b.id], FLAG_REJECT)
    assert catalog.get(c.id).rating == 5
    assert {r.id for r in catalog.query(LibraryFilter(min_rating=5))} == {c.id}
    assert {r.id for r in catalog.query(LibraryFilter(flag="picked"))} == {a.id}
    assert {r.id for r in catalog.query(LibraryFilter(flag="not_rejected"))} == {a.id, c.id}
    assert [r.filename for r in catalog.query(LibraryFilter(sort="rating", descending=True))][0] == "c.jpg"


def test_folder_source_includes_subfolders(catalog, photo_tree):
    import_folder(catalog, str(photo_tree))
    folders = {f.name: f for f in catalog.folders()}
    top = folders[str(photo_tree / "2024")]
    assert len(catalog.query(LibraryFilter(source="folder", source_id=top.id))) == 3
    trip = folders[str(photo_tree / "2024" / "trip")]
    assert len(catalog.query(LibraryFilter(source="folder", source_id=trip.id))) == 1
    catalog.remove_folder(top.id)
    assert catalog.count() == 0
    assert (photo_tree / "2024" / "a.jpg").exists()  # files untouched


def test_tags_and_collections(catalog, photo_tree):
    import_folder(catalog, str(photo_tree))
    a, b, c = sorted(catalog.query(), key=lambda r: r.filename)
    catalog.add_tags([a.id, b.id], ["beach", "Family", " "])
    catalog.add_tags([c.id], ["BEACH"])  # case-insensitive
    assert catalog.tags_for([a.id, c.id]) == {"beach": 2, "Family": 1}
    beach = next(t for t in catalog.tags() if t.name == "beach")
    assert beach.count == 3
    assert len(catalog.query(LibraryFilter(source="tag", source_id=beach.id))) == 3
    assert {r.id for r in catalog.query(LibraryFilter(text="fam"))} == {a.id, b.id}
    catalog.remove_tag([a.id], "family")
    assert catalog.tags_for([a.id]) == {"beach": 1}

    cid = catalog.create_collection("Portfolio")
    catalog.add_to_collection(cid, [a.id, c.id, a.id])
    assert catalog.collections()[0].count == 2
    catalog.remove_from_collection(cid, [a.id])
    assert [r.id for r in catalog.query(LibraryFilter(source="collection", source_id=cid))] == [c.id]
    catalog.delete_collection(cid)
    assert catalog.count() == 3


def test_edits_are_versioned(catalog, photo_tree):
    import_folder(catalog, str(photo_tree))
    rec = catalog.query()[0]
    edits.rotate(catalog, [rec.id], 90)
    edits.rotate(catalog, [rec.id], 90)
    edits.rotate(catalog, [rec.id], -90)
    assert catalog.get(rec.id).rotate == 90
    assert [h[1] for h in catalog.edit_history(rec.id)] == ["Rotate right", "Rotate right", "Rotate left"]
    assert catalog.edit_history(rec.id)[1][2] == {"rotate": 180}


def test_like_wildcards_escaped(catalog, photo_tree):
    import_folder(catalog, str(photo_tree))
    assert catalog.query(LibraryFilter(text="%")) == []


def test_pixel_size_respects_orientation(qapp, catalog, tmp_path):
    from conftest import make_jpeg

    make_jpeg(tmp_path / "in" / "r.jpg", size=(600, 400), orientation=6)
    import_folder(catalog, str(tmp_path / "in"))
    rec = catalog.query()[0]
    assert (rec.width, rec.height) == (400, 600)


def test_cr3_metadata(tmp_path):
    from conftest import make_cr3
    from foto.importer import read_metadata

    meta = read_metadata(str(make_cr3(tmp_path / "x.CR3")))
    assert (meta["make"], meta["model"], meta["lens"]) == ("Canon", "Canon EOS R5m2", "RF24-70mm F2.8 L IS USM")
    assert (meta["iso"], meta["capture_time"]) == (3200, "2026-06-08T23:38:10")
