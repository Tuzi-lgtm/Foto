import os
import shutil
import sqlite3
import time

import pytest

from foto.backup.base import remote_rel, sha256_file
from foto.backup.engine import BackupEngine
from foto.backup.targets import BackupTarget, delete_target, list_targets, save_target
from foto.catalog import LibraryFilter
from foto.importer import import_folder


def test_remote_rel():
    assert remote_rel("/Users/me/Pictures/a.arw") == "Users/me/Pictures/a.arw"


def _setup(catalog, photo_tree, tmp_path, kind="folder"):
    import_folder(catalog, str(photo_tree))
    dest = tmp_path / "nas"
    dest.mkdir()
    cfg = {"path": str(dest)} if kind == "folder" else {"remote": f":local:{dest}"}
    cfg["keep_snapshots"] = 2
    t = BackupTarget(None, "NAS", kind, cfg)
    save_target(catalog, t)
    return t, dest


def test_folder_backup_incremental(catalog, photo_tree, tmp_path):
    target, dest = _setup(catalog, photo_tree, tmp_path)
    r = BackupEngine(catalog, target).run()
    assert (r.copied, r.unchanged, r.failed) == (3, 0, 0), r.errors
    for rec in catalog.query():
        copy = dest / "originals" / remote_rel(rec.path)
        assert sha256_file(str(copy)) == sha256_file(rec.path)
    assert all(rec.backed_up for rec in catalog.query())
    assert catalog.query(LibraryFilter(backup="not_backed_up")) == []

    r = BackupEngine(catalog, target).run(snapshot=False)
    assert (r.copied, r.unchanged) == (0, 3)

    a = photo_tree / "2024" / "a.jpg"
    a.write_bytes(a.read_bytes() + b"\0")  # original changed on disk
    os.utime(a, ns=(time.time_ns(), time.time_ns() + 10**9))
    r = BackupEngine(catalog, target).run(snapshot=False)
    assert (r.copied, r.unchanged) == (1, 2)
    assert (dest / "originals" / remote_rel(str(a))).read_bytes() == a.read_bytes()


def test_offline_original_and_selection(catalog, photo_tree, tmp_path):
    target, dest = _setup(catalog, photo_tree, tmp_path)
    recs = catalog.query()
    os.remove(recs[0].path)
    r = BackupEngine(catalog, target).run(image_ids=[recs[0].id, recs[1].id], snapshot=False)
    assert (r.copied, r.missing) == (1, 1)


def test_catalog_snapshots_pruned(catalog, photo_tree, tmp_path):
    target, dest = _setup(catalog, photo_tree, tmp_path)
    engine = BackupEngine(catalog, target)
    names = []
    for _ in range(3):
        names.append(engine.snapshot_catalog())
        time.sleep(1.05)
    kept = sorted(p.name for p in (dest / "catalog").glob("*.db"))
    assert kept == names[-2:]
    snap = sqlite3.connect(dest / "catalog" / kept[-1])
    assert snap.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 3
    snap.close()


def test_unreachable_target(catalog, photo_tree, tmp_path):
    from foto.backup.base import BackupError

    import_folder(catalog, str(photo_tree))
    t = BackupTarget(None, "gone", "folder", {"path": str(tmp_path / "not-mounted")})
    save_target(catalog, t)
    with pytest.raises(BackupError):
        BackupEngine(catalog, t).run()
    delete_target(catalog, t.id)
    assert list_targets(catalog) == []


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone not installed")
def test_rclone_backup(catalog, photo_tree, tmp_path):
    target, dest = _setup(catalog, photo_tree, tmp_path, kind="rclone")
    r = BackupEngine(catalog, target).run()
    assert (r.copied, r.failed) == (3, 0), r.errors
    for rec in catalog.query():
        assert (dest / "originals" / remote_rel(rec.path)).read_bytes() == open(rec.path, "rb").read()
    assert len(list((dest / "catalog").glob("catalog-*.db"))) == 1
    r = BackupEngine(catalog, target).run(snapshot=False)
    assert (r.copied, r.unchanged) == (0, 3)
