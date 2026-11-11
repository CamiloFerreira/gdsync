"""Comprobación e instalación de actualizaciones (incluidas las obligatorias por seguridad).

Cada release publica `update.json`:
    {
      "version": "1.2.0",
      "min_supported": "1.1.3",      # versiones menores tienen vulnerabilidades conocidas
      "advisory": "texto del aviso",
      "url": "https://github.com/.../releases/tag/v1.2.0",
      "assets": {"deb": {"url": ..., "sha256": ...}, "appimage": {"url": ..., "sha256": ...}}
    }
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import core
from . import __version__

REPO = "CamiloFerreira/gdsync"
UPDATE_URL = f"https://github.com/{REPO}/releases/latest/download/update.json"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
CACHE = core.STATE_DIR / "update.json"  # compartido con el CLI (security_gate)
ALLOWED_PREFIX = f"https://github.com/{REPO}/releases/download/"


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def install_kind() -> str:
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if str(core.PKG_DIR).startswith("/usr/lib/gdsync"):
        return "deb"
    return "source"


@dataclass
class UpdateInfo:
    current: str
    latest: str
    min_supported: str
    advisory: str
    url: str
    assets: dict

    @property
    def available(self) -> bool:
        return parse_version(self.latest) > parse_version(self.current)

    @property
    def mandatory(self) -> bool:
        return parse_version(self.current) < parse_version(self.min_supported)


def _from_json(data: dict) -> UpdateInfo:
    return UpdateInfo(
        current=__version__, latest=str(data.get("version", "0.0.0")),
        min_supported=str(data.get("min_supported", "0.0.0")), advisory=str(data.get("advisory", "")),
        url=str(data.get("url", RELEASES_URL)), assets=data.get("assets") or {},
    )


def cached() -> UpdateInfo | None:
    try:
        return _from_json(json.loads(CACHE.read_text()))
    except Exception:
        return None


def check(max_age: int = 0) -> UpdateInfo | None:
    """Descarga update.json (o usa caché si es reciente). None si no hay info."""
    if max_age and CACHE.exists() and time.time() - CACHE.stat().st_mtime < max_age:
        return cached()
    try:
        req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": f"gdsync/{__version__}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read(256 * 1024)
        data = json.loads(raw)
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(CACHE)
        return _from_json(data)
    except Exception:
        return cached()


def _download(asset: dict, dest: Path, progress=None) -> Path:
    url, sha = asset.get("url", ""), asset.get("sha256", "")
    if not url.startswith(ALLOWED_PREFIX) or not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        raise core.CoreError("Datos de actualización inválidos")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": f"gdsync/{__version__}"})
    with urllib.request.urlopen(req, timeout=60) as r, dest.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(256 * 1024):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    if h.hexdigest() != sha:
        dest.unlink(missing_ok=True)
        raise core.CoreError("La descarga no coincide con su SHA-256: actualización cancelada")
    return dest


def apply(info: UpdateInfo, progress=None) -> str:
    """Instala la actualización. Devuelve el comando para relanzar la app ('' si no aplica)."""
    kind = install_kind()
    if kind == "appimage":
        target = Path(os.environ["APPIMAGE"])
        tmp = target.with_name(f".{target.name}.new")
        _download(info.assets["appimage"], tmp, progress)
        tmp.chmod(0o755)
        tmp.replace(target)  # reemplazo atómico del propio AppImage
        return str(target)
    if kind == "deb":
        d = Path(tempfile.mkdtemp(prefix="gdsync-update-"))
        deb = _download(info.assets["deb"], d / f"gdsync_{info.latest}_all.deb", progress)
        deb.chmod(0o644)
        d.chmod(0o755)
        # pkexec muestra el diálogo de contraseña del sistema
        r = subprocess.run(["pkexec", "apt-get", "install", "-y", "--allow-downgrades", str(deb)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise core.CoreError(core.last_err(r, "No se pudo instalar el paquete"))
        return "/usr/bin/gdsync-desktop"
    raise core.CoreError("Instalación desde código fuente: actualiza con «git pull»")
