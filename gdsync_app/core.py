"""Lógica sin interfaz: config, rclone, systemd, tareas en segundo plano."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

APP = "gdsync"
HOME = Path.home()
PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent
GDSYNC = PROJECT_DIR / "gdsync"
ICONS_DIR = PROJECT_DIR / "data" / "icons"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / APP
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / APP
CONFIG_FILE = CONFIG_DIR / "config"
FILTERS_FILE = CONFIG_DIR / "filters.txt"
LOG_FILE = STATE_DIR / "sync.log"
BIN_DIR = HOME / ".local/bin"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd/user"
AUTOSTART_FILE = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "autostart" / f"{APP}.desktop"

ENV = dict(os.environ)
ENV["PATH"] = f"{BIN_DIR}:{ENV.get('PATH', '')}"
ENV["NO_COLOR"] = "1"

DEFAULTS = {"REMOTE_NAME": "gdrive", "REMOTE_PATH": "", "LOCAL_PATH": "", "INTERVAL": "5min"}
CONFIG_KEYS = tuple(DEFAULTS)
DEFAULT_FILTERS = """# Reglas de exclusión rclone (https://rclone.org/filtering/)
- .~lock.*
- ~$*
- *.tmp
- *.swp
- .DS_Store
- Thumbs.db
- desktop.ini
- .Trash-*/**
# Restos de Google Drive para escritorio (Windows/macOS)
- .sync-exclude.lst
- .tmp.drivedownload/**
- .tmp.driveupload/**
"""
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class CoreError(Exception):
    pass


def run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)


def rclone_bin() -> str | None:
    return shutil.which("rclone", path=ENV["PATH"])


def last_err(r: subprocess.CompletedProcess, default: str) -> str:
    txt = ANSI.sub("", (r.stderr or r.stdout or "")).strip()
    return txt.splitlines()[-1] if txt else default


# ------------------------------------------------------------------ config

def read_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, _, raw = line.partition("=")
            if key in CONFIG_KEYS:
                try:
                    parts = shlex.split(raw)
                    cfg[key] = parts[0] if parts else ""
                except ValueError:
                    cfg[key] = raw
    return cfg


def _write_private(path: Path, text: str) -> None:
    """Escritura atómica con permisos 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_config(cfg: dict) -> None:
    _write_private(CONFIG_FILE, "".join(f"{k}={shlex.quote(str(cfg.get(k, '')))}\n" for k in CONFIG_KEYS))


ROOT = "/"  # REMOTE_PATH para sincronizar toda «Mi unidad»


def remote_label(remote_path: str) -> str:
    return "Mi unidad (todo)" if remote_path == ROOT else remote_path


def is_configured(cfg: dict | None = None) -> bool:
    cfg = cfg or read_config()
    return bool(cfg["REMOTE_PATH"] and cfg["LOCAL_PATH"])


def valid_remote_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name or ""))


# Subcarpetas de HOME que nunca deben sincronizarse (configuración / credenciales)
SENSITIVE_HOME_DIRS = (".ssh", ".gnupg", ".pki", ".config", ".local", ".cache", ".mozilla",
                       "snap", ".var", ".password-store")
MOUNT_ROOTS = ("/media", "/mnt", "/run/media")


def check_local_path(path: str) -> str:
    """Devuelve la ruta normalizada o lanza CoreError. Mismas reglas que el CLI."""
    if not path or "\n" in path or "\0" in path:
        raise CoreError("Ruta local inválida")
    path = os.path.expanduser(path.strip())
    if not os.path.isabs(path):
        raise CoreError("La carpeta local debe ser una ruta absoluta")
    real = Path(os.path.realpath(path))
    home = Path(os.path.realpath(HOME))
    in_home = home in real.parents
    in_mount = any(Path(m) in real.parents and real != Path(m) for m in MOUNT_ROOTS)
    if not (in_home or in_mount):
        raise CoreError(f"La carpeta local debe estar dentro de {home} o en un disco montado (/media, /mnt)")
    if in_home and real.relative_to(home).parts[0] in SENSITIVE_HOME_DIRS:
        raise CoreError("No se puede sincronizar una carpeta de configuración o credenciales")
    return path


def free_space(path: str) -> int | None:
    """Bytes libres en el disco donde quedaría `path` (sube hasta un ancestro existente)."""
    p = Path(os.path.expanduser(path or "")).absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def save_config(remote: str, remote_path: str, local_path: str, interval: str) -> str:
    old = read_config()
    remote_path = ROOT if remote_path == ROOT else remote_path.strip("/")
    if not valid_remote_name(remote):
        raise CoreError("Cuenta inválida")
    if not remote_path:
        raise CoreError("Elige una carpeta de Drive")
    if ".." in remote_path.split("/"):
        raise CoreError("Ruta remota inválida")
    if "\n" in remote_path:
        raise CoreError("Ruta remota inválida")
    lp = check_local_path(local_path)
    if not re.fullmatch(r"\d{1,5}(s|min|h|d)", interval):
        raise CoreError("Intervalo inválido")
    cfg = {"REMOTE_NAME": remote, "REMOTE_PATH": remote_path, "LOCAL_PATH": lp, "INTERVAL": interval}
    target_changed = any(old[k] != cfg[k] for k in ("REMOTE_NAME", "REMOTE_PATH", "LOCAL_PATH"))
    if target_changed and sync_in_progress():
        raise CoreError(BUSY_MSG)
    try:
        Path(lp).mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as e:
        raise CoreError(f"No se pudo crear la carpeta local: {e.strerror}") from e
    if not os.access(lp, os.W_OK | os.X_OK):
        raise CoreError(f"No tienes permiso de escritura en {lp}. Cambia sus permisos o elige otra carpeta.")
    write_config(cfg)

    msgs = ["Configuración guardada"]
    if target_changed:
        reset_bisync_state()
        msgs.append("el próximo sync unirá ambos lados")
    if timer_enabled() and old["INTERVAL"] != interval:
        set_timer(True)
    return " · ".join(msgs)


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return pid > 0


def sync_in_progress() -> bool:
    """True si hay un bisync corriendo: unit systemd activa o lock de rclone con PID vivo."""
    if systemd_props(f"{APP}.service", ["ActiveState"]).get("ActiveState") in ("activating", "deactivating"):
        return True
    for lck in (STATE_DIR / "bisync").glob("*.lck"):
        try:
            if _pid_alive(json.loads(lck.read_text()).get("PID")):
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


BUSY_MSG = "Hay una sincronización en curso. Espera a que termine (o cancélala) y vuelve a guardar."


def reset_bisync_state() -> None:
    # Borrar el workdir con un bisync en marcha lo deja inconsistente
    if sync_in_progress():
        raise CoreError(BUSY_MSG)
    shutil.rmtree(STATE_DIR / "bisync", ignore_errors=True)


def bisync_initialized() -> bool:
    d = STATE_DIR / "bisync"
    return d.exists() and any(d.glob("*.lst"))


def read_filters() -> str:
    return FILTERS_FILE.read_text() if FILTERS_FILE.exists() else ""


# Grupos de reglas que la app muestra como interruptores
FILTER_PRESETS = {
    "temp": ("Archivos temporales y de bloqueo", "Bloqueos de LibreOffice/Office, *.tmp, *.swp",
             ["- .~lock.*", "- ~$*", "- *.tmp", "- *.swp"]),
    "system": ("Archivos del sistema", "Papelera, .DS_Store, Thumbs.db, desktop.ini",
               ["- .DS_Store", "- Thumbs.db", "- desktop.ini", "- .Trash-*/**"]),
    "gdrive": ("Restos de Google Drive para escritorio", ".sync-exclude.lst y carpetas .tmp.drive*",
               ["- .sync-exclude.lst", "- .tmp.drivedownload/**", "- .tmp.driveupload/**"]),
    "dev": ("Carpetas de programación", "node_modules, .git, venv, __pycache__, site-packages… (miles de archivos pequeños)",
            ["- node_modules/**", "- .git/**", "- .venv/**", "- venv/**", "- __pycache__/**", "- site-packages/**"]),
}
FILTERS_HEADER = "# Reglas de exclusión rclone (https://rclone.org/filtering/)"
_RE_FOLDER_RULE = re.compile(r"^- /(.+)/\*\*$")


def parse_filters(text: str) -> tuple[set[str], list[str], list[str]]:
    """Separa el archivo de filtros en (presets activos, carpetas excluidas, reglas propias)."""
    lines = [l.strip() for l in text.splitlines()]
    rules = {l for l in lines if l and not l.startswith("#")}
    presets = {k for k, (_, _, r) in FILTER_PRESETS.items() if set(r) <= rules}
    taken = {r for k in presets for r in FILTER_PRESETS[k][2]}
    folders, custom = [], []
    for l in lines:
        if not l or l.startswith("#") or l in taken:
            continue
        m = _RE_FOLDER_RULE.match(l)
        if m and m.group(1) not in folders:
            folders.append(m.group(1))
        elif l not in custom:
            custom.append(l)
    return presets, folders, custom


def build_filters(presets: set[str], folders: list[str], custom: list[str]) -> str:
    """Inverso de parse_filters. Las reglas propias van primero: en rclone gana la primera que coincide."""
    out = [FILTERS_HEADER]
    if custom:
        out += ["# Reglas propias"] + custom
    for k, (title, _, rules) in FILTER_PRESETS.items():
        if k in presets:
            out += [f"# {title}"] + rules
    if folders:
        out += ["# Carpetas excluidas"] + [f"- /{f.strip('/')}/**" for f in folders]
    return "\n".join(out) + "\n"


def folder_rule_path(local_root: str, chosen: str) -> str:
    """Ruta relativa (para «- /ruta/**») de una carpeta elegida dentro de la carpeta local."""
    root = Path(os.path.realpath(local_root))
    p = Path(os.path.realpath(chosen))
    if p == root or root not in p.parents:
        raise CoreError("Elige una carpeta dentro de tu carpeta sincronizada")
    rel = p.relative_to(root).as_posix()
    if any(c in rel for c in "*?[]{}\n\\"):
        raise CoreError("Esa carpeta tiene caracteres especiales; agrégala en «Reglas avanzadas»")
    return rel


def save_filters(text: str) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s and not s.startswith("#") and s[:2] not in ("- ", "+ "):
            raise CoreError(f"Línea {i}: debe empezar por «- », «+ » o «#»")
    if "\0" in text:
        raise CoreError("Texto inválido")
    if sync_in_progress():
        raise CoreError(BUSY_MSG)
    _write_private(FILTERS_FILE, text if not text or text.endswith("\n") else text + "\n")
    reset_bisync_state()


# ------------------------------------------------------------------ rclone

def rclone_version() -> str | None:
    rc = rclone_bin()
    if not rc:
        return None
    try:
        return run([rc, "version"], timeout=10).stdout.splitlines()[0].replace("rclone ", "")
    except Exception:
        return None


def list_remotes() -> list[dict]:
    rc = rclone_bin()
    if not rc:
        return []
    r = run([rc, "config", "dump"], timeout=15)
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return []
    # Solo nombre y tipo: nunca tokens.
    return [{"name": n, "type": c.get("type", "?")} for n, c in sorted(data.items())]


def about(remote: str) -> dict:
    if not valid_remote_name(remote):
        raise CoreError("Cuenta inválida")
    r = run([rclone_bin() or "rclone", "about", f"{remote}:", "--json"], timeout=30)
    if r.returncode != 0:
        raise CoreError(last_err(r, "No se pudo leer la cuota"))
    return json.loads(r.stdout)


def list_dirs(remote: str, path: str) -> dict:
    path = path.strip("/")
    if not valid_remote_name(remote) or ".." in path.split("/"):
        raise CoreError("Ruta inválida")
    r = run([rclone_bin() or "rclone", "lsjson", f"{remote}:{path}", "--drive-skip-gdocs", "--no-modtime"], timeout=60)
    if r.returncode != 0:
        raise CoreError(last_err(r, "Error listando Drive"))
    items = json.loads(r.stdout or "[]")
    return {
        "dirs": sorted((i["Name"] for i in items if i.get("IsDir")), key=str.lower),
        "files": sum(1 for i in items if not i.get("IsDir")),
    }


def delete_remote(name: str) -> None:
    if not valid_remote_name(name):
        raise CoreError("Cuenta inválida")
    cfg = read_config()
    if name == cfg["REMOTE_NAME"] and cfg["REMOTE_PATH"]:
        raise CoreError("Esa cuenta está en uso por la sincronización")
    r = run([rclone_bin() or "rclone", "config", "delete", name], timeout=15)
    if r.returncode != 0:
        raise CoreError(last_err(r, "Error eliminando la cuenta"))


# ----------------------------------------------------------------- systemd

def systemd_props(unit: str, props: list[str]) -> dict:
    try:
        r = run(["systemctl", "--user", "show", unit, "-p", ",".join(props)], timeout=10)
    except Exception:
        return {}
    return dict(line.partition("=")[::2] for line in r.stdout.splitlines())


def timer_enabled() -> bool:
    return systemd_props(f"{APP}.timer", ["UnitFileState"]).get("UnitFileState") == "enabled"


def set_timer(enabled: bool) -> None:
    if enabled and not is_configured():
        raise CoreError("Primero elige la carpeta")
    r = run([str(GDSYNC), "enable" if enabled else "disable"], timeout=30)
    if r.returncode != 0:
        raise CoreError(last_err(r, "Error con systemd"))


RESUME_UNIT = f"{APP}-resume"


def pause_for(seconds: int) -> None:
    """Pausa el timer (sin deshabilitarlo) y programa su reanudación con un timer transitorio.
    Si se reinicia el equipo, el timer habilitado vuelve solo: la pausa nunca queda olvidada."""
    if not timer_enabled():
        raise CoreError("La sincronización automática no está activada")
    until = datetime.now() + timedelta(seconds=max(60, int(seconds)))
    run(["systemctl", "--user", "stop", f"{RESUME_UNIT}.timer"], timeout=15)
    # on-calendar (hora absoluta): así systemd informa la hora real de reanudación
    r = run(["systemd-run", "--user", f"--unit={RESUME_UNIT}", f"--on-calendar={until:%Y-%m-%d %H:%M:%S}",
             "--timer-property=AccuracySec=30s", "--description=Reanuda gdsync tras una pausa",
             "systemctl", "--user", "start", f"{APP}.timer"], timeout=15)
    if r.returncode != 0:
        raise CoreError(last_err(r, "No se pudo programar la reanudación"))
    r = run(["systemctl", "--user", "stop", f"{APP}.timer"], timeout=15)
    if r.returncode != 0:
        raise CoreError(last_err(r, "No se pudo pausar"))


def resume() -> None:
    run(["systemctl", "--user", "stop", f"{RESUME_UNIT}.timer"], timeout=15)
    r = run(["systemctl", "--user", "start", f"{APP}.timer"], timeout=15)
    if r.returncode != 0:
        raise CoreError(last_err(r, "No se pudo reanudar"))


def seconds_until_tomorrow(hour: int = 8, now: datetime | None = None) -> int:
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int((target - now).total_seconds())


def stop_service() -> None:
    r = run(["systemctl", "--user", "stop", f"{APP}.service"], timeout=30)
    if r.returncode != 0:
        raise CoreError(last_err(r, "No se pudo detener la sincronización"))


def service_installed() -> bool:
    return (UNIT_DIR / f"{APP}.service").exists()


def parse_systemd_time(v: str | None) -> datetime | None:
    if not v or v in ("n/a", "0"):
        return None
    m = re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", v)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None


# --------------------------------------------------------------- autostart

def desktop_exec_quote(path: str) -> str:
    """Cita una ruta para la clave Exec= de un .desktop (spec freedesktop)."""
    if re.fullmatch(r"[A-Za-z0-9/._+-]+", path):
        return path
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in path).replace("%", "%%")
    return f'"{escaped}"'


def autostart_enabled() -> bool:
    return AUTOSTART_FILE.exists()


def set_autostart(enabled: bool, exec_cmd: str) -> None:
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\nType=Application\nName=Google Drive Sync\n"
        f"Exec={desktop_exec_quote(exec_cmd)} --background\nIcon=io.github.gdsync\n"
        "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n"
    )


# ------------------------------------------------------------------ status

@dataclass
class Status:
    rclone: str | None = None
    configured: bool = False
    config: dict = field(default_factory=dict)
    timer_enabled: bool = False
    next_run: datetime | None = None
    service_running: bool = False
    service_failed: bool = False
    initialized: bool = False
    last_sync: datetime | None = None
    last_ok: bool | None = None
    paused_until: datetime | None = None
    has_account: bool = False
    problem: "Diagnosis | None" = None
    shared_client_id: bool = False
    postponed: str | None = None   # archivo en uso que hizo posponer el último sync

    @property
    def state(self) -> str:
        """ok | syncing | error | paused | setup"""
        if not self.rclone or not self.configured:
            return "setup"
        if self.service_running:
            return "syncing"
        if self.service_failed or self.last_ok is False:
            return "error"
        if not self.timer_enabled or not self.initialized or self.paused_until:
            return "paused"
        return "ok"


LOG_TS = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)")
POSTPONED = "Sync pospuesto: archivo en uso:"  # lo escribe gdsync al esperar por un archivo abierto
# Línea que cierra una ejecución (las escribe rclone, o gdsync al abortar antes de rclone)
RESULT_OK = ("Bisync successful",)
RESULT_BAD = ("Bisync aborted", "Bisync critical error", "gdsync ERROR:")
DRIVE_CLIENT_ID_URL = "https://rclone.org/drive/#making-your-own-client-id"


def _is_result(line: str) -> bool:
    return any(k in line for k in RESULT_OK + RESULT_BAD)


def last_run_lines(lines: list[str]) -> list[str]:
    """Líneas de la última ejecución terminada (entre el resultado anterior y el último)."""
    idx = [i for i, l in enumerate(lines) if _is_result(l)]
    if not idx:
        return []
    end = idx[-1]
    start = idx[-2] + 1 if len(idx) > 1 else 0
    return lines[start:end + 1]


@dataclass
class Diagnosis:
    title: str
    detail: str
    action: str | None = None   # resync | reauth | open-folder | url | None
    action_label: str = ""
    url: str = ""


def diagnose(lines: list[str]) -> Diagnosis:
    """Traduce los errores de una ejecución a algo entendible y una acción sugerida."""
    text = "\n".join(lines)
    low = text.lower()
    m = re.search(r"gdsync ERROR: (.+)", text)
    if m and "disco" in m.group(1).lower():
        return Diagnosis("El disco no está conectado", m.group(1).strip(), "open-folder", "Abrir carpeta")
    if any(k in low for k in ("invalid_grant", "token expired", "couldn't fetch token", "empty token",
                              "oauth2: cannot fetch token", "error 401")):
        return Diagnosis("Hay que volver a conectar tu cuenta",
                         "Google rechazó el acceso (la autorización venció o se revocó).",
                         "reauth", "Reconectar cuenta")
    if "permission denied" in low or (m and "sin permiso" in m.group(1).lower()):
        p = re.search(r"open ([^:]+): permission denied|([^ ]+): permission denied", text)
        where = (p.group(1) or p.group(2)) if p else ""
        return Diagnosis("Sin permiso para escribir en la carpeta local",
                         (f"No se puede escribir en {where}. " if where else "")
                         + "Revisa los permisos de la carpeta (o si el disco está montado en solo lectura).",
                         "open-folder", "Abrir carpeta")
    if "no space left on device" in low:
        return Diagnosis("El disco está lleno", "No queda espacio en la carpeta local.", "open-folder", "Abrir carpeta")
    if "too many deletes" in low or "safety abort" in low:
        return Diagnosis("Se detuvo por seguridad: demasiados borrados",
                         "Desapareció una gran parte de los archivos de un lado. Si el disco se desconectó, "
                         "conéctalo y vuelve a sincronizar. «Forzar resync» une ambos lados y restaura lo que falte.",
                         "resync", "Forzar resync")
    if any(k in low for k in ("must run --resync", "cannot find prior path1 or path2 listings",
                              "out of sync", "resync is required", "bisync critical error")):
        return Diagnosis("La sincronización necesita repararse",
                         "El historial de bisync quedó inconsistente. Repararlo une ambos lados desde cero; no borra archivos.",
                         "resync", "Reparar")
    if any(k in low for k in ("quota exceeded", "ratelimitexceeded", "userratelimitexceeded", "error 429")):
        return Diagnosis("Google limitó las peticiones",
                         "Se volverá a intentar en la próxima sincronización. Si pasa seguido, crea tu propio "
                         "client_id de Google Drive (el compartido de rclone tiene límites y se está retirando).",
                         "url", "Cómo hacerlo", DRIVE_CLIENT_ID_URL)
    if any(k in low for k in ("no such host", "dial tcp", "network is unreachable", "i/o timeout",
                              "connection refused", "connection reset", "tls handshake timeout")):
        return Diagnosis("Sin conexión con Google Drive",
                         "Revisa tu conexión a internet. Se volverá a intentar automáticamente.")
    if m:
        return Diagnosis("La sincronización no pudo empezar", m.group(1).strip())
    errs = [l for l in lines if "ERROR" in l]
    last = errs[-1] if errs else (lines[-1] if lines else "")
    last = re.sub(r"^\S+ \S+ (ERROR|NOTICE|INFO)\s*:\s*", "", last).strip()
    return Diagnosis("Hubo un problema al sincronizar", last[:300] or "Revisa la pestaña Actividad.")


def uses_shared_client_id(lines: list[str]) -> bool:
    return any("shared Google Drive client_id" in l for l in lines)


def tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max(8192, n * 300)))
        data = f.read().decode("utf-8", "replace")
    return data.splitlines()[-n:]


def get_status() -> Status:
    cfg = read_config()
    t = systemd_props(f"{APP}.timer", ["UnitFileState", "NextElapseUSecRealtime", "ActiveState"])
    s = systemd_props(f"{APP}.service", ["ActiveState", "Result"])
    st = Status(
        rclone=rclone_version(), config=cfg, configured=is_configured(cfg),
        timer_enabled=t.get("UnitFileState") == "enabled",
        next_run=parse_systemd_time(t.get("NextElapseUSecRealtime")),
        service_running=s.get("ActiveState") == "activating",
        service_failed=s.get("Result") not in (None, "", "success"),
        initialized=bisync_initialized(),
    )
    if st.rclone and not st.configured:
        st.has_account = any(r["type"] == "drive" for r in list_remotes())
    if st.timer_enabled and t.get("ActiveState") == "inactive":
        rt = systemd_props(f"{RESUME_UNIT}.timer", ["ActiveState", "NextElapseUSecRealtime"])
        if rt.get("ActiveState") == "active":
            st.paused_until = parse_systemd_time(rt.get("NextElapseUSecRealtime"))
            st.next_run = None
    lines = tail(LOG_FILE, 400)
    for line in reversed(lines):
        ok = any(k in line for k in RESULT_OK)
        bad = any(k in line for k in RESULT_BAD)
        if ok or bad:
            st.last_ok = ok
            m = LOG_TS.match(line)
            if m:
                st.last_sync = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
            break
    for line in reversed(lines):
        if _is_result(line):
            break
        if POSTPONED in line:
            st.postponed = line.split(POSTPONED, 1)[1].strip()
            break
    run_lines = last_run_lines(lines)
    if st.last_ok is False:
        st.problem = diagnose(run_lines)
    elif st.service_failed:
        st.problem = Diagnosis("La última sincronización falló", "Revisa la pestaña Actividad para ver el detalle.")
    st.shared_client_id = uses_shared_client_id(run_lines or lines[-200:])
    return st


# ------------------------------------------------------- progreso/cambios

PHASES = (  # (texto en el log de rclone bisync, fase legible, dirección de las copias)
    ("Synching Path1", "Revisando archivos", None),
    ("Building Path1 and Path2 listings", "Revisando archivos", None),
    ("checking for diffs", "Buscando cambios", None),
    ("Copying Path2 files to Path1", "Subiendo a Drive", "up"),
    ("Copying Path1 files to Path2", "Descargando de Drive", "down"),
    ("Applying changes", "Aplicando cambios", None),
    ("queued copies to - Path1", "Subiendo a Drive", "up"),
    ("queued copies to - Path2", "Descargando de Drive", "down"),
    ("queued deletes on - Path1", "Borrando en Drive", "up"),
    ("queued deletes on - Path2", "Borrando en este equipo", "down"),
    ("Updating listings", "Guardando estado", None),
)
_RE_BYTES = re.compile(r"^Transferred:\s+([\d.]+ \S*B) / ([\d.]+ \S*B), (\d+)%(?:, ([\d.]+ \S*B/s), ETA (\S+))?")
_RE_FILES = re.compile(r"^Transferred:\s+(\d+) / (\d+), (\d+)%\s*$")
_RE_CHECKS = re.compile(r"^Checks:\s+(\d+) / (\d+), (\d+)%(?:, Listed (\d+))?")
_RE_ITEM = re.compile(r"^(\S+ \S+) INFO  : (.+): (Copied \(new\)|Copied \(replaced existing\)|Updated [^:]*|Deleted|"
                      r"Moved[^:]*|Made directory[^:]*|Removing directory)$")
_RE_CONFLICT_COPY = re.compile(r"\.conflict\d+(\.[^/]*)?$")
_RE_CONFLICT = re.compile(r"New or changed in both paths\s+-\s+(.+?)\s*$")


@dataclass
class Change:
    when: str
    path: str
    kind: str          # new | updated | deleted | moved | dir | conflict
    direction: str | None  # up | down | None


class SyncTracker:
    """Lee las líneas de sync.log y mantiene progreso, cambios recientes y conflictos."""

    MAX = 300

    def __init__(self):
        self.changes: list[Change] = []
        self.reset()

    def reset(self):
        self.running = False
        self.phase = ""
        self.direction: str | None = None
        self.current = ""
        self.bytes_done = self.bytes_total = ""
        self.percent: int | None = None
        self.speed = self.eta = ""
        self.files_done = self.files_total = 0
        self.checks = self.listed = 0
        self.run_conflicts: list[str] = []

    def feed(self, lines: list[str]) -> bool:
        """Procesa líneas; True si algo visible cambió."""
        changed = False
        for line in lines:
            changed |= self._line(line)
        return changed

    def _add(self, ch: Change):
        self.changes.append(ch)
        del self.changes[:-self.MAX]

    def _line(self, line: str) -> bool:
        if any(k in line for k in RESULT_OK + RESULT_BAD):
            self.reset()
            return True
        for key, phase, direction in PHASES:
            if key in line:
                if key in ("Synching Path1", "Building Path1 and Path2 listings") and not self.running:
                    self.reset()  # empieza una ejecución nueva
                self.running = True
                self.phase, self.direction = phase, direction
                return True
        line_s = line.strip()
        m = _RE_BYTES.match(line_s)
        if m:
            self.running = True
            self.bytes_done, self.bytes_total, pct, self.speed, self.eta = m.group(1), m.group(2), m.group(3), m.group(4) or "", m.group(5) or ""
            self.percent = int(pct)
            return True
        m = _RE_FILES.match(line_s)
        if m:
            self.files_done, self.files_total = int(m.group(1)), int(m.group(2))
            return True
        m = _RE_CHECKS.match(line_s)
        if m:
            self.checks, self.listed = int(m.group(1)), int(m.group(4) or 0)
            return True
        m = _RE_CONFLICT.search(line)
        if m:
            path = m.group(1)
            if path not in self.run_conflicts:
                self.run_conflicts.append(path)
            self._add(Change(line[:19], path, "conflict", None))
            return True
        m = _RE_ITEM.match(line)
        if m:
            when, path, what = m.groups()
            kind = ("new" if what.startswith("Copied (new") else "updated" if what.startswith(("Copied", "Updated"))
                    else "deleted" if what.startswith(("Deleted", "Removing")) else "moved" if what.startswith("Moved")
                    else "dir")
            self.current = path
            if kind != "deleted" and _RE_CONFLICT_COPY.search(path):
                kind = "conflict"  # copia «.conflictN» que deja --conflict-loser num
            if kind != "dir":
                self._add(Change(when, path, kind, self.direction))
            return True
        return False

    def summary(self) -> str:
        """Texto corto del progreso: «Subiendo a Drive · 1.2 GiB de 3.6 GiB (33 %) · 8 MiB/s · quedan 2m»."""
        parts = [self.phase or "Sincronizando"]
        if self.bytes_total and self.percent is not None and not self.bytes_total.startswith("0 "):
            parts.append(f"{self.bytes_done} de {self.bytes_total} ({self.percent} %)")
            if self.speed and not self.speed.startswith("0 "):
                parts.append(self.speed)
            if self.eta and self.eta not in ("-", "0s"):
                parts.append(f"quedan {self.eta}")
        elif self.listed:
            parts.append(f"{self.listed} archivos revisados")
        return " · ".join(parts)


# -------------------------------------------------------------------- jobs

class Job:
    """Proceso en segundo plano. Callbacks se llaman desde otro hilo."""

    def __init__(self, kind: str, cmd: list[str], on_line: Callable[[str], None],
                 on_done: Callable[["Job"], None]):
        self.kind, self.cmd = kind, cmd
        self.on_line, self.on_done = on_line, on_done
        self.code: int | None = None
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def ok(self) -> bool:
        return self.code == 0

    def _run(self):
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, env=ENV, bufsize=1)
            assert self.proc.stdout
            for line in self.proc.stdout:
                self.on_line(ANSI.sub("", line.rstrip("\n")))
            self.code = self.proc.wait()
        except Exception as e:  # noqa: BLE001
            self.on_line(f"Error: {e}")
            self.code = -1
        self.on_done(self)

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.cancelled = True
            if self.kind == "sync" and self.cmd[:2] == ["systemctl", "--user"]:
                run(["systemctl", "--user", "stop", f"{APP}.service"], timeout=20)
            self.proc.terminate()


FORCE_ONCE = STATE_DIR / "force-once"  # el próximo sync no espera por archivos en uso


def request_force_sync() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FORCE_ONCE.touch()


def job_command(kind: str, arg: str = "") -> list[str]:
    if kind == "sync":
        # Vía systemd si está instalado: nunca se pisa con el timer.
        if service_installed():
            return ["systemctl", "--user", "start", f"{APP}.service"]
        return [str(GDSYNC), "sync"]
    if kind in ("dry-run", "resync", "install-rclone"):
        return [str(GDSYNC), kind]
    if kind == "reauth":
        if not valid_remote_name(arg):
            raise CoreError("Cuenta inválida")
        return [rclone_bin() or "rclone", "config", "reconnect", f"{arg}:", "--auto-confirm"]
    if kind == "add-remote":
        if not valid_remote_name(arg):
            raise CoreError("Nombre inválido: usa letras, números, - o _")
        return [rclone_bin() or "rclone", "config", "create", arg, "drive", "scope=drive"]
    raise CoreError(f"Acción desconocida: {kind}")


class LogFollower:
    """Sigue sync.log (como tail -f) y entrega líneas nuevas."""

    def __init__(self, on_lines: Callable[[list[str]], None], interval: float = 0.7):
        self.on_lines, self.interval = on_lines, interval
        self.pos = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
                if size < self.pos:  # rotado/borrado
                    self.pos = 0
                if size > self.pos:
                    with LOG_FILE.open("rb") as f:
                        f.seek(self.pos)
                        chunk = f.read()
                    nl = chunk.rfind(b"\n")
                    if nl >= 0:
                        self.pos += nl + 1
                        self.on_lines(chunk[: nl + 1].decode("utf-8", "replace").splitlines())
            except OSError:
                pass
            time.sleep(self.interval)


def fmt_bytes(b: float | None) -> str:
    if b is None:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while b >= 1024 and i < len(units) - 1:
        b /= 1024
        i += 1
    return f"{b:.1f} {units[i]}" if b < 10 and i else f"{round(b)} {units[i]}"


def fmt_rel(d: datetime | None) -> str:
    if not d:
        return "—"
    s = int((d - datetime.now()).total_seconds())
    a = abs(s)
    t = f"{a} s" if a < 60 else f"{round(a / 60)} min" if a < 3600 else f"{round(a / 3600)} h" if a < 86400 else f"{round(a / 86400)} d"
    return f"en {t}" if s >= 0 else f"hace {t}"
