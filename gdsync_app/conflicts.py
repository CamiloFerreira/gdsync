"""Detección y resolución de conflictos y duplicados (sin interfaz).

Dos tipos de problema:

* **Conflicto**: rclone bisync encontró el mismo archivo cambiado en los dos lados.
  Se queda la versión más reciente con el nombre original y renombra la otra a
  ``nombre.conflictN.ext``. El usuario decide qué conservar.
* **Duplicado**: varios elementos hermanos que son "el mismo" salvo un sufijo
  ``(1)``, ``(1) (1)``… o caracteres que Windows/macOS no admiten (``:``, ``?``…).
  Los crea otro cliente de Drive (p. ej. Drive para escritorio en Windows) al no
  poder representar el nombre, y se multiplican en cada ronda.

Todas las operaciones trabajan sobre la carpeta local; el siguiente sync las
lleva a Drive (lo borrado acaba en la papelera de Drive, no se pierde).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import core

CONFLICT_RE = re.compile(r"^(?P<stem>.+?)\.conflict(?P<n>\d+)(?P<ext>\.[^.]+)?$")
DUP_SUFFIX = re.compile(r"(?:\s*\(\d+\))+$")
WIN_INVALID = re.compile(r'[:*?"<>|\\]')
MAX_ISSUES = 300
PREVIEW_BYTES = 64 * 1024
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")


@dataclass
class Entry:
    path: Path
    is_dir: bool
    size: int = 0          # bytes (carpetas: suma de archivos)
    files: int = 0         # solo carpetas
    mtime: datetime | None = None

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Issue:
    kind: str                      # "conflict" | "duplicate"
    folder: Path                   # carpeta que lo contiene
    entries: list[Entry] = field(default_factory=list)
    # conflict: entries[0] = original (puede no existir), entries[1] = copia
    original_missing: bool = False
    identical: bool = False

    @property
    def title(self) -> str:
        if self.kind == "conflict":
            return self.entries[0].name
        return min((e.name for e in self.entries), key=len)

    @property
    def key(self) -> str:
        return f"{self.kind}:" + "|".join(sorted(str(e.path) for e in self.entries))


# ------------------------------------------------------------------ rutas

def local_root() -> Path:
    lp = core.read_config()["LOCAL_PATH"]
    if not lp:
        raise core.CoreError("Sin carpeta configurada")
    return Path(os.path.realpath(core.check_local_path(lp)))


def ensure_inside(path: Path, root: Path | None = None) -> Path:
    """Ruta real dentro de la carpeta sincronizada (nunca la raíz) o CoreError."""
    root = root or local_root()
    p = Path(os.path.realpath(path)) if not os.path.islink(path) else Path(os.path.realpath(path.parent)) / path.name
    if root not in p.parents:
        raise core.CoreError("La ruta está fuera de la carpeta sincronizada")
    return p


def valid_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name or "\n" in name or len(name.encode()) > 255:
        raise core.CoreError("Nombre inválido")
    return name


def non_portable(name: str) -> bool:
    """Caracteres o espacios que Drive para Windows/macOS no puede representar."""
    stem = os.path.splitext(name)[0]
    return bool(WIN_INVALID.search(name)) or name != name.strip() or stem != stem.rstrip(" .")


def portable_name(name: str) -> str:
    """Nombre sin caracteres que Windows/macOS no admiten."""
    stem, ext = os.path.splitext(name)
    # Sin colapsar espacios: «A: b» -> «A  b», igual que hace Drive para Windows
    stem = WIN_INVALID.sub(" ", stem).strip(" .")
    return (stem + ext.strip()) or "sin nombre"


# --------------------------------------------------------------- escaneo

def conflict_original(name: str) -> str | None:
    m = CONFLICT_RE.match(name)
    if not m:
        return None
    return m.group("stem") + (m.group("ext") or "")


def dup_key(name: str, is_dir: bool) -> str:
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    stem = DUP_SUFFIX.sub("", stem)
    stem = re.sub(r"\s+", " ", WIN_INVALID.sub(" ", stem)).strip()
    return ("d:" if is_dir else "f:") + (stem + ext).casefold()


def _is_variant(name: str, is_dir: bool) -> bool:
    stem = name if is_dir else os.path.splitext(name)[0]
    return bool(DUP_SUFFIX.search(stem))


def _entry(p: Path, st: os.stat_result | None = None) -> Entry:
    st = st or p.lstat()
    e = Entry(p, stat.S_ISDIR(st.st_mode), mtime=datetime.fromtimestamp(st.st_mtime))
    if e.is_dir:
        latest = 0.0  # fecha de los archivos: la de la carpeta cambia con cada sync
        for dp, _dn, fn in os.walk(p):
            for f in fn:
                try:
                    s = os.lstat(os.path.join(dp, f))
                except OSError:
                    continue
                e.files += 1
                e.size += s.st_size
                latest = max(latest, s.st_mtime)
        e.mtime = datetime.fromtimestamp(latest or st.st_mtime)
    else:
        e.size = st.st_size
    return e


def same_content(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return file_hash(a) == file_hash(b)
    except OSError:
        return False


def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_signature(d: Path) -> dict[str, int]:
    out = {}
    for dp, _dn, fn in os.walk(d):
        for f in fn:
            fp = os.path.join(dp, f)
            try:
                out[os.path.relpath(fp, d)] = os.lstat(fp).st_size
            except OSError:
                pass
    return out


def scan(root: Path | None = None) -> list[Issue]:
    """Busca conflictos y duplicados en la carpeta local. Puede tardar: llamar fuera de GTK."""
    root = root or local_root()
    issues: list[Issue] = []
    if not root.is_dir():
        return issues
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        here = Path(dirpath)
        names = {n: True for n in dirnames} | {n: False for n in filenames}
        conflict_copies = set()
        # -- conflictos
        for n in sorted(filenames):
            orig = conflict_original(n)
            if orig is None:
                continue
            conflict_copies.add(n)
            op, cp = here / orig, here / n
            try:
                ce = _entry(cp)
            except OSError:
                continue
            exists = op.is_file() and not op.is_symlink()
            oe = _entry(op) if exists else Entry(op, False)
            issues.append(Issue("conflict", here, [oe, ce], original_missing=not exists,
                                identical=exists and same_content(op, cp)))
        # -- duplicados
        groups: dict[str, list[str]] = {}
        for n, is_dir in names.items():
            if n in conflict_copies or (here / n).is_symlink():
                continue
            groups.setdefault(dup_key(n, is_dir), []).append(n)
        for members in groups.values():
            if len(members) < 2:
                continue
            is_dir = names[members[0]]
            # Exigir un "base" (sin (N)) o un nombre no portable: evita marcar foto (1)/foto (2) legítimas
            if all(_is_variant(m, is_dir) for m in members) and not any(non_portable(m) for m in members):
                continue
            entries = []
            for m in sorted(members, key=lambda s: (len(s), s)):
                try:
                    entries.append(_entry(here / m))
                except OSError:
                    pass
            if len(entries) < 2:
                continue
            if is_dir:
                sigs = [_tree_signature(e.path) for e in entries]
                identical = all(s == sigs[0] for s in sigs[1:])
            else:
                identical = all(same_content(entries[0].path, e.path) for e in entries[1:])
            issues.append(Issue("duplicate", here, entries, identical=identical))
        if len(issues) >= MAX_ISSUES:
            break
    return issues[:MAX_ISSUES]


def _suffix_count(e: Entry) -> int:
    m = DUP_SUFFIX.search(e.name if e.is_dir else e.path.stem)
    return m.group().count("(") if m else 0


def suggested_keeper(issue: Issue) -> Entry:
    """En un duplicado: el nombre portable, sin (N), y el más reciente."""
    return min(issue.entries, key=lambda e: (non_portable(e.name), _suffix_count(e),
                                             -(e.mtime.timestamp() if e.mtime else 0)))


# ---------------------------------------------------------------- acciones

def _trash_gio(path: Path) -> None:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
    Gio.File.new_for_path(str(path)).trash(None)


TRASH = _trash_gio  # reemplazable en los tests


def trash(path: Path) -> None:
    p = ensure_inside(path)
    if not p.exists() and not p.is_symlink():
        raise core.CoreError(f"Ya no existe: {p.name}")
    try:
        TRASH(p)
    except Exception as e:  # noqa: BLE001
        raise core.CoreError(f"No se pudo mover a la papelera: {e}") from e


def rename(path: Path, new_name: str) -> Path:
    p = ensure_inside(path)
    new_name = valid_name(new_name)
    dst = p.with_name(new_name)
    if dst == p:
        return p
    if dst.exists() or dst.is_symlink():
        raise core.CoreError(f"Ya existe «{new_name}» en esa carpeta")
    os.rename(p, dst)
    return dst


def free_conflict_name(path: Path) -> Path:
    stem, ext = os.path.splitext(path.name)
    n = 1
    while True:
        cand = path.with_name(f"{stem}.conflict{n}{ext}")
        if not cand.exists():
            return cand
        n += 1


def resolve_conflict(issue: Issue, action: str, new_name: str = "") -> str:
    """action: keep_original | keep_copy | keep_both"""
    if issue.kind != "conflict":
        raise core.CoreError("No es un conflicto")
    orig, copy = issue.entries[0].path, issue.entries[1].path
    root = local_root()
    ensure_inside(orig, root)
    ensure_inside(copy, root)
    if not copy.exists():
        raise core.CoreError("La copia en conflicto ya no existe")
    if action == "keep_original":
        if not orig.exists():
            raise core.CoreError("El original ya no existe: usa «Quedarme con esta»")
        trash(copy)
        return f"Conservado «{orig.name}»"
    if action == "keep_copy":
        if orig.exists():
            trash(orig)
        os.rename(copy, orig)
        return f"«{orig.name}» ahora tiene el contenido de la copia"
    if action == "keep_both":
        dst = rename(copy, new_name)
        return f"Conservados ambos: «{orig.name}» y «{dst.name}»"
    raise core.CoreError(f"Acción desconocida: {action}")


def _newer(a: Path, b: Path) -> bool:
    return a.stat().st_mtime > b.stat().st_mtime


def merge_into(src: Path, dst: Path, newest: bool = False) -> dict:
    """Mueve el contenido de la carpeta src a dst y manda src a la papelera.

    Archivos idénticos se descartan. Los que difieren se guardan como copia en
    conflicto (nombre.conflictN.ext) para decidir después o, con newest=True, gana
    el más reciente. Nada se borra sin pasar por la papelera.
    """
    root = local_root()
    src, dst = ensure_inside(src, root), ensure_inside(dst, root)
    if not src.is_dir() or not dst.is_dir() or src == dst or src in dst.parents or dst in src.parents:
        raise core.CoreError("Solo se pueden unir dos carpetas distintas")
    stats = {"moved": 0, "same": 0, "conflicts": 0, "replaced": 0}
    for dirpath, dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        tdir = dst / rel
        if tdir.exists() and not tdir.is_dir():
            tdir = free_conflict_name(tdir)
        tdir.mkdir(parents=True, exist_ok=True)
        for d in list(dirnames):
            if (Path(dirpath) / d).is_symlink():
                dirnames.remove(d)
        for f in filenames:
            s, t = Path(dirpath) / f, tdir / f
            if s.is_symlink():
                continue
            if not t.exists():
                os.rename(s, t)
                stats["moved"] += 1
            elif t.is_file() and same_content(s, t):
                stats["same"] += 1
            elif newest and t.is_file():
                if _newer(s, t):
                    trash(t)
                    os.rename(s, t)
                stats["replaced"] += 1
            else:
                os.rename(s, free_conflict_name(t))
                stats["conflicts"] += 1
    trash(src)
    return stats


def resolve_duplicates(issue: Issue, keeper: Path, final_name: str = "", newest: bool = False) -> str:
    """Une/elimina todos los demás miembros del grupo en keeper y le pone final_name.

    newest=True: si el contenido difiere, gana la versión más reciente (la otra va a la papelera).
    """
    if issue.kind != "duplicate":
        raise core.CoreError("No es un duplicado")
    root = local_root()
    keeper = ensure_inside(keeper, root)
    others = [p for p in (ensure_inside(e.path, root) for e in issue.entries) if p != keeper]
    total = {"moved": 0, "same": 0, "conflicts": 0, "replaced": 0}
    for o in others:
        if not o.exists():
            continue
        if keeper.is_dir():
            for k, v in merge_into(o, keeper, newest).items():
                total[k] += v
        else:
            if newest and not same_content(o, keeper):
                if _newer(o, keeper):
                    trash(keeper)
                    os.rename(o, keeper)
                else:
                    trash(o)
                total["replaced"] += 1
            elif not same_content(o, keeper):
                os.rename(o, free_conflict_name(keeper))
                total["conflicts"] += 1
            else:
                trash(o)
                total["same"] += 1
    if final_name and final_name != keeper.name:
        keeper = rename(keeper, final_name)
    msg = f"Unificado en «{keeper.name}»"
    if total["conflicts"]:
        msg += f" · {total['conflicts']} archivo(s) distintos quedan como conflicto para revisar"
    return msg


def clean_name(e: Entry) -> str:
    """Nombre sin «(1)», «(1) (1)»… ni caracteres que Windows/macOS no admiten."""
    if e.is_dir:
        return portable_name(DUP_SUFFIX.sub("", e.name))
    stem, ext = os.path.splitext(e.name)
    return portable_name(DUP_SUFFIX.sub("", stem) + ext)


AUTO_MIN_AGE = 300  # no tocar nada modificado hace menos de 5 min (puede estar en uso)


def in_use(e: Entry) -> bool:
    """Abierto en un editor (LibreOffice, MS Office, vim, emacs) o modificado hace poco."""
    if e.mtime and datetime.now().timestamp() - e.mtime.timestamp() < AUTO_MIN_AGE:
        return True
    d, n = e.path.parent, e.name
    return any((d / lk).exists() for lk in (f".~lock.{n}#", f"~${n}", f".{n}.swp", f".#{n}"))


def auto_clean(issues: list[Issue] | None = None) -> dict:
    """Resuelve sin preguntar: gana la versión más reciente y el resto va a la papelera.

    Conflictos: se queda la más reciente con el nombre original.
    Duplicados: se unen en uno con el nombre limpio (sin «(1)» ni «:»).
    Se salta lo modificado hace poco. Devuelve {"conflicts", "duplicates", "skipped", "errors"}.
    """
    issues = scan() if issues is None else issues
    out = {"conflicts": 0, "duplicates": 0, "skipped": 0, "errors": []}
    for i in issues:
        if any(in_use(e) for e in i.entries if e.path.exists()):
            out["skipped"] += 1
            continue
        try:
            if i.kind == "conflict":
                orig, copy = i.entries[0].path, i.entries[1].path
                keep_copy = i.original_missing or (not i.identical and _newer(copy, orig))
                resolve_conflict(i, "keep_copy" if keep_copy else "keep_original")
                out["conflicts"] += 1
            else:
                keeper = suggested_keeper(i)
                final = clean_name(keeper)
                taken = (keeper.path.parent / final).exists() and final not in {e.name for e in i.entries}
                resolve_duplicates(i, keeper.path, "" if taken else final, newest=True)
                out["duplicates"] += 1
        except (core.CoreError, OSError) as e:
            out["errors"].append(f"{i.title}: {e}")
    return out


# --------------------------------------------------------------- contenido

def preview(path: Path) -> dict:
    """{'kind': 'text'|'image'|'dir'|'binary'|'missing', ...} para mostrar en el diálogo."""
    p = ensure_inside(path)
    if not p.exists():
        return {"kind": "missing"}
    if p.is_dir():
        items = []
        for dp, dn, fn in os.walk(p):
            dn.sort()
            for f in sorted(fn):
                items.append(os.path.relpath(os.path.join(dp, f), p))
                if len(items) >= 200:
                    break
            if len(items) >= 200:
                break
        return {"kind": "dir", "items": items}
    with p.open("rb") as f:
        data = f.read(PREVIEW_BYTES)
    if data[:8] in (b"\x89PNG\r\n\x1a\n",) or data[:3] == b"\xff\xd8\xff" or data[:6] in (b"GIF87a", b"GIF89a") \
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return {"kind": "image"}
    if b"\0" not in data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            if e.start < len(data) - 4:  # no es solo un carácter cortado al final
                return {"kind": "binary"}
            text = data[:e.start].decode("utf-8")
        return {"kind": "text", "text": text, "truncated": p.stat().st_size > PREVIEW_BYTES}
    return {"kind": "binary"}


# ------------------------------------------------------------ Google Drive

def drive_url(path: Path | None = None) -> str:
    """URL web de Google Drive para un archivo/carpeta local sincronizado (o la carpeta raíz)."""
    cfg = core.read_config()
    if not core.valid_remote_name(cfg["REMOTE_NAME"]) or not cfg["REMOTE_PATH"]:
        raise core.CoreError("Sin carpeta configurada")
    remote = cfg["REMOTE_PATH"].strip("/")
    is_dir = True
    if path is not None:
        root = local_root()
        p = ensure_inside(path, root)
        is_dir = p.is_dir()
        remote = f"{remote}/{p.relative_to(root).as_posix()}"
    parent, _, name = remote.rpartition("/")
    # «lsjson --stat» no da el ID de una carpeta: se lista la carpeta padre y se busca el nombre
    try:
        r = core.run([core.rclone_bin() or "rclone", "lsjson", "--no-modtime", "--no-mimetype",
                      "--dirs-only" if is_dir else "--files-only", f"{cfg['REMOTE_NAME']}:{parent}"], timeout=60)
    except Exception as e:  # noqa: BLE001  (TimeoutExpired, OSError)
        raise core.CoreError("Google Drive no respondió a tiempo") from e
    if r.returncode != 0:
        raise core.CoreError("Aún no está en Google Drive (sincroniza primero)")
    try:
        items = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as e:
        raise core.CoreError("Respuesta inesperada de rclone") from e
    fid = next((i.get("ID", "") for i in items if isinstance(i, dict) and i.get("Name") == name), "")
    if not fid:
        raise core.CoreError("Aún no está en Google Drive (sincroniza primero)")
    if not DRIVE_ID.fullmatch(fid):
        raise core.CoreError("Google Drive no devolvió un identificador válido")
    if is_dir:
        return f"https://drive.google.com/drive/folders/{fid}"
    return f"https://drive.google.com/file/d/{fid}/view"
