"""File types Foto knows how to import."""

RAW_EXTS = {
    ".arw", ".srf", ".sr2",          # Sony
    ".cr2", ".cr3", ".crw",          # Canon
    ".nef", ".nrw",                  # Nikon
    ".raf",                          # Fujifilm
    ".dng",                          # Adobe / Leica / phones
    ".orf", ".rw2", ".pef", ".srw", ".3fr", ".iiq", ".erf", ".rwl",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
SUPPORTED_EXTS = RAW_EXTS | IMAGE_EXTS


def is_raw(path: str) -> bool:
    return _ext(path) in RAW_EXTS


def is_supported(path: str) -> bool:
    return _ext(path) in SUPPORTED_EXTS


def _ext(path: str) -> str:
    dot = path.rfind(".")
    return path[dot:].lower() if dot >= 0 else ""
