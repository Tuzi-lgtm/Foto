from pathlib import Path

from foto.app import parse_args


def test_tilde_expanded():
    args = parse_args(["--catalog", "~/FotoTest.fotocat", "--import", "~/shoot"])
    assert args.catalog == Path.home() / "FotoTest.fotocat"
    assert Path(args.import_folder) == Path.home() / "shoot"
