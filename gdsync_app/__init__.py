"""gdsync: app de escritorio para sincronizar una carpeta de Google Drive."""
from pathlib import Path


def _read_version() -> str:
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


__version__ = _read_version()
