# Foto

A personal, Lightroom-style photo library. Python · PySide6 · OpenGL · rawpy (LibRaw) · OpenColorIO · SQLite.

Originals are **never written**. Ratings, flags, tags, collections and edit settings live only in the
catalog database; the viewer applies edits at display time.

## Phase 1 (this release)

- **Import** folders recursively (JPEG, TIFF, PNG and raw: Sony ARW, Canon CR3/CR2, Nikon NEF, Fuji RAF, DNG, plus other LibRaw formats). Re-importing skips known files.
- **Fast thumbnails**: raw files use the camera's embedded JPEG, JPEGs use DCT-scaled decoding. Disk cache (320 px thumbs, 2560 px previews) keyed by path + size + mtime, plus a memory cache. Visible cells load first.
- **Recently Added and By Date** in the library panel: each import run is its own batch, and every photo is
  browsable by year › month › day of capture. Both are virtual, so nothing is reorganized on disk.
- **Ratings, flags, tags, collections**, filters (rating, flag, backup state, text across name/camera/lens/tag) and sorting.
- **Loupe** and **Compare** in an OpenGL viewer: pan and zoom, true 1:1 (loads full resolution on demand), and synced zoom in compare.
- **OCIO display pipeline** on the GPU: `$OCIO` or the built-in CG config. Choose display, view and input space; there's also a display-only exposure control.
- **Non-destructive edits**: rotation for now, stored as versioned JSON with full history.
- **Backup** to a NAS or the cloud: incremental and hash-verified, with catalog snapshots.

## Setup

Python 3.10+ on macOS or Windows.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
foto                               # or: python -m foto
foto --catalog ~/Pictures/Main.fotocat --import ~/Pictures/2025
```

The default catalog lives in `~/Library/Application Support/Foto/Default.fotocat` (macOS) or
`%APPDATA%\Foto\Default.fotocat` (Windows). A catalog folder holds `catalog.db` and `cache/`.
You can delete `cache/` at any time; it rebuilds itself.

## Keys

| Key | Action | Key | Action |
|---|---|---|---|
| `0`–`5` | Rating | `G` / `E` / `C` | Grid / Loupe / Compare |
| `P` / `X` / `U` | Pick / Reject / Unflag | `Esc`, double-click beside photo | Back to grid |
| `←` / `→` | Previous / next (compare: active side) | `Z`, double-click on photo | Fit ↔ 1:1 |
| `Ctrl+[` / `Ctrl+]` | Rotate left / right | scroll, drag | Zoom at cursor, pan |
| `Ctrl+K` | Add tags | `B` | Add to target collection |
| `Ctrl+I` | Import folder | `Tab` | Toggle side panels |
| `Ctrl+=` / `Ctrl+-` / `Ctrl+0` | Viewer exposure | `Ctrl+Shift+S` | Swap compare sides |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo | `Ctrl+C` / `Ctrl+V` | Copy / paste edit settings |
| `Ctrl+A` / `Ctrl+D` | Select all / none | `Ctrl+Shift+I` | Select inverse |
| `Delete` | Remove from catalog | `Ctrl+,` | Preferences |
| `F6` | Show / hide filmstrip | `F11` | Show / hide secondary window |
| `Shift+G` / `Shift+E` | Secondary window: grid / detail | `Ctrl+Shift+F` | Secondary window full screen |

In compare mode, click a side to make it active. Ratings, flags and arrows then apply to that side.

In loupe and compare, a **filmstrip** runs along the bottom. Drag the divider above it to resize it; the thumbnails scale
with it. In compare, clicking the filmstrip or pressing the arrows changes the active side. The **secondary window**
(View › Secondary Window) opens on your other monitor. It shows either a grid or the current photo in detail, and it shares the
selection with the main window. Rating, flag and arrow keys work in it too.

Undo covers ratings, flags, rotation, revert, pasted settings, tags and collection membership.
Removing photos from the catalog can't be undone. It only moves files to the Recycle Bin (Trash) if you tick that box.

**Preferences** hold per-user settings: cache location and size limit (least recently viewed previews are
dropped first), the catalog opened when no `--catalog` is given, decoder threads, and the OCIO config.

## Backup

Open **Backup → Backup Targets…** to add a target:

- **NAS / mounted folder**: mount the Synology share first (Finder → Connect to Server `smb://nas/photo`,
  or map a drive in Windows), then pick the folder. This path uses native copies: each file is written to a
  temp name, re-read to check its SHA-256, and then renamed into place.
- **Cloud via rclone**: install [rclone](https://rclone.org/install/) and run `rclone config` once
  to add Google Cloud Storage, Google Drive, S3, B2, or your Synology over SFTP/WebDAV. Then enter
  `remote:path` (e.g. `gcs:my-bucket/foto`). Foto stores only that string; credentials stay in rclone.
  Each batch is uploaded with `rclone copy` and verified with `rclone check`.

Layout at the destination:

```
<target>/originals/<absolute path of the original>   e.g. originals/Users/me/Pictures/2025/DSC1000.ARW
<target>/catalog/catalog-YYYYMMDD-HHMMSS.db           last N snapshots (default 10)
```

Runs are incremental. A file is re-sent only when its size or mtime changed, and an interrupted run
picks up where it stopped. Foto never deletes backed-up originals. The ☁ badge and the "Not backed up"
filter show what is covered.

## Architecture

```
foto/
  catalog/     SQLite schema + migrations (PRAGMA user_version), Catalog API
  importer.py  scan + EXIF (exifread, LibRaw fallback)
  imaging/     decode (rawpy / QImageReader), disk cache, threaded ImageService
  color/       OCIO config, GLSL + LUT texture extraction
  backup/      folder + rclone backends, incremental engine, targets
  edits.py     non-destructive edit operations
  ui/          main window, grid, GL image view, loupe/compare, panels, dialogs
```

The viewer keeps its state resolution-independent (scale relative to fit, centre in normalized image
coords). So a thumbnail can be swapped for a preview and then full-res without the view jumping, and compare
mode syncs by passing that state across.

## Tests

```bash
pytest                                 # catalog, importer, cache, OCIO, backup (incl. rclone if installed)
FOTO_SAMPLE_RAW=/path/to/file.ARW pytest tests/test_imaging.py   # check a real camera file
```

## Roadmap

- **Phase 2 — Develop**: linear raw decode into a float GPU texture; exposure, white balance and tone in shaders;
  OCIO scene-linear working space; history/snapshot UI.
- **Phase 3**: export, XMP sidecars, smart collections, EXR/OIIO, restore from backup.
