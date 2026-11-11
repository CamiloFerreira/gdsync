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
from datetime import datetime
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


def save_config(remote: str, remote_path: str, local_path: str, interval: str) -> str:
    old = read_config()
    remote_path = remote_path.strip("/")
    if not valid_remote_name(remote):
        raise CoreError("Cuenta inválida")
    if not remote_path:
        raise CoreError("Elige una carpeta de Drive (no la raíz)")
    if ".." in remote_path.split("/"):
        raise CoreError("Ruta remota inválida")
    if "\n" in remote_path:
        raise CoreError("Ruta remota inválida")
    lp = check_local_path(local_path)
    if not re.fullmatch(r"\d{1,5}(s|min|h|d)", interval):
        raise CoreError("Intervalo inválido")
    Path(lp).mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = {"REMOTE_NAME": remote, "REMOTE_PATH": remote_path, "LOCAL_PATH": lp, "INTERVAL": interval}
    write_config(cfg)

    msgs = ["Configuración guardada"]
    if any(old[k] != cfg[k] for k in ("REMOTE_NAME", "REMOTE_PATH", "LOCAL_PATH")):
        reset_bisync_state()
        msgs.append("el próximo sync unirá ambos lados")
    if timer_enabled() and old["INTERVAL"] != interval:
        set_timer(True)
    return " · ".join(msgs)


def reset_bisync_state() -> None:
    shutil.rmtree(STATE_DIR / "bisync", ignore_errors=True)


def bisync_initialized() -> bool:
    d = STATE_DIR / "bisync"
    return d.exists() and any(d.glob("*.lst"))


def read_filters() -> str:
    return FILTERS_FILE.read_text() if FILTERS_FILE.exists() else ""


def save_filters(text: str) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s and not s.startswith("#") and s[:2] not in ("- ", "+ "):
            raise CoreError(f"Línea {i}: debe empezar por «- », «+ » o «#»")
    if "\0" in text:
        raise CoreError("Texto inválido")
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

    @property
    def state(self) -> str:
        """ok | syncing | error | paused | setup"""
        if not self.rclone or not self.configured:
            return "setup"
        if self.service_running:
            return "syncing"
        if self.service_failed or self.last_ok is False:
            return "error"
        if not self.timer_enabled or not self.initialized:
            return "paused"
        return "ok"


LOG_TS = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)")


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
    t = systemd_props(f"{APP}.timer", ["UnitFileState", "NextElapseUSecRealtime"])
    s = systemd_props(f"{APP}.service", ["ActiveState", "Result"])
    st = Status(
        rclone=rclone_version(), config=cfg, configured=is_configured(cfg),
        timer_enabled=t.get("UnitFileState") == "enabled",
        next_run=parse_systemd_time(t.get("NextElapseUSecRealtime")),
        service_running=s.get("ActiveState") == "activating",
        service_failed=s.get("Result") not in (None, "", "success"),
        initialized=bisync_initialized(),
    )
    for line in reversed(tail(LOG_FILE, 400)):
        ok = "Bisync successful" in line
        bad = "Bisync aborted" in line or "Bisync critical error" in line
        if ok or bad:
            st.last_ok = ok
            m = LOG_TS.match(line)
            if m:
                st.last_sync = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
            break
    return st


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


def job_command(kind: str, arg: str = "") -> list[str]:
    if kind == "sync":
        # Vía systemd si está instalado: nunca se pisa con el timer.
        if service_installed():
            return ["systemctl", "--user", "start", f"{APP}.service"]
        return [str(GDSYNC), "sync"]
    if kind in ("dry-run", "resync", "install-rclone"):
        return [str(GDSYNC), kind]
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
