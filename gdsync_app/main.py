"""Aplicación: instancia única, icono de bandeja, sondeo de estado, tareas."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import __version__, core, updater  # noqa: E402
from .tray import MenuItem, Tray  # noqa: E402
from .window import MainWindow, bg  # noqa: E402

APP_ID = "io.github.gdsync"
SETTINGS_FILE = core.CONFIG_DIR / "desktop.json"
TRAY_ICON = {"ok": "gdsync-tray-ok-symbolic", "syncing": "gdsync-tray-sync-symbolic",
             "error": "gdsync-tray-error-symbolic", "paused": "gdsync-tray-paused-symbolic",
             "setup": "gdsync-tray-paused-symbolic"}
STATE_TEXT = {"ok": "Todo sincronizado", "syncing": "Sincronizando…", "error": "Error de sincronización",
              "paused": "Sincronización en pausa", "setup": "Sin configurar"}
JOB_TITLES = {"sync": "Sincronizando…", "dry-run": "Simulando (no se cambia nada)…", "resync": "Resync completo…",
              "install-rclone": "Instalando rclone…", "add-remote": "Conectando cuenta (mira tu navegador)…"}


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Arrancar solo en la bandeja", None)
        self.win: MainWindow | None = None
        self.tray: Tray | None = None
        self.job: core.Job | None = None
        self.status: core.Status | None = None
        self.prev_state: str | None = None
        self.settings = self._load_settings()
        self._quota_remote = None
        self._polling = False
        self.update: updater.UpdateInfo | None = None
        self.blocked = False
        self._mandatory_dlg = None
        self._updating = False

    # --------------------------------------------------------- arranque
    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.IconTheme.get_for_display(Gtk.Window().get_display()).add_search_path(str(core.ICONS_DIR))
        Gtk.Window.set_default_icon_name(APP_ID)
        for name, cb in (("quit", lambda *_: self.quit_app()), ("open-folder", lambda *_: self.open_folder()),
                         ("about", lambda *_: self.about()), ("show", lambda *_: self.show_window()),
                         ("sync", lambda *_: self.start_job("sync"))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.sync", ["<Control>r"])
        self.hold()  # seguir vivo en la bandeja sin ventana

        self.win = MainWindow(self)
        self.tray = Tray(APP_ID, "Google Drive Sync", str(core.ICONS_DIR), self.toggle_window)
        self._build_tray_menu()
        core.LogFollower(lambda lines: GLib.idle_add(self._on_log_lines, lines))
        self.refresh()
        GLib.timeout_add_seconds(4, self._poll)
        # Actualizaciones: caché al instante (funciona offline), red a los 3 s y cada 6 h
        cached = updater.cached()
        if cached:
            self._on_update_info(cached)
        GLib.timeout_add_seconds(3, self.check_updates)
        GLib.timeout_add_seconds(6 * 3600, self.check_updates)
        # Tras actualizar, regenerar las units para que apunten a la versión nueva
        if core.timer_enabled():
            bg(lambda: core.run([str(core.GDSYNC), "enable"], timeout=30))

    def do_command_line(self, cmdline):
        opts = cmdline.get_options_dict().end().unpack()
        if not opts.get("background"):
            self.show_window()
        return 0

    # ----------------------------------------------------------- ventana
    def show_window(self):
        self.win.set_visible(True)
        self.win.present()

    def toggle_window(self):
        if self.win.get_visible() and self.win.is_active():
            self.win.set_visible(False)
        else:
            self.show_window()

    def on_window_hidden(self):
        if not self.settings.get("hint_shown"):
            self.settings_set("hint_shown", True)
            self.notify("Sigue funcionando en segundo plano",
                        "gdsync está en la barra superior. Usa «Salir» en su menú para cerrarlo del todo.")

    def quit_app(self):
        self.release()
        self.quit()

    # ------------------------------------------------------------ estado
    def _poll(self):
        if not self._polling:
            self.refresh()
        return True

    def refresh(self):
        self._polling = True

        def done(st: core.Status):
            self._polling = False
            self._apply_status(st)
        bg(core.get_status, done, lambda e: setattr(self, "_polling", False))

    def _apply_status(self, st: core.Status):
        self.status = st
        job_sync = self.job is not None and self.job.kind in ("sync", "resync")
        state = "syncing" if job_sync or st.service_running else st.state
        # Notificar errores del sync automático (transición a error)
        if self.prev_state == "syncing" and state == "error" and not job_sync:
            self.notify("Error de sincronización", "Revisa la pestaña Actividad.", urgent=True)
        self.prev_state = state
        self.win.update_status(st, job_sync or st.service_running)
        self.tray.set_icon(TRAY_ICON["error" if self.blocked else state], self._tooltip(st, state))
        self._build_tray_menu()
        if st.configured and st.rclone and self._quota_remote != st.config["REMOTE_NAME"]:
            self.load_quota()

    def _tooltip(self, st: core.Status, state: str) -> str:
        if self.blocked:
            return "Actualización de seguridad obligatoria"
        if not st.configured:
            return STATE_TEXT[state]
        last = f"\nÚltima: {core.fmt_rel(st.last_sync)}" if st.last_sync else ""
        return f"{STATE_TEXT[state]}\n{st.config['REMOTE_NAME']}:{st.config['REMOTE_PATH']}{last}"

    def load_quota(self):
        remote = (self.status.config if self.status else core.read_config())["REMOTE_NAME"]
        self._quota_remote = remote
        bg(lambda: core.about(remote), lambda q: self.win.update_quota(remote, q),
           lambda e: self.win.update_quota(remote, None, str(e)))

    def _build_tray_menu(self):
        st = self.status
        state = self.prev_state or "setup"
        configured = bool(st and st.configured)
        busy = self.job is not None or bool(st and st.service_running)
        items = [MenuItem(STATE_TEXT[state], enabled=False)]
        if self.blocked:
            items = [MenuItem("⚠ Actualización de seguridad obligatoria", self.show_mandatory)]
        elif self.update and self.update.available:
            items.append(MenuItem(f"Actualizar a {self.update.latest}", self.do_update, enabled=not self._updating))
        if st and st.last_sync:
            items.append(MenuItem(f"Última: {core.fmt_rel(st.last_sync)}", enabled=False))
        items += [
            MenuItem(separator=True),
            MenuItem("Abrir gdsync", self.show_window),
            MenuItem("Sincronizar ahora", lambda: self.start_job("sync"), enabled=configured and not busy),
            MenuItem("Abrir carpeta local", self.open_folder, enabled=configured),
            MenuItem("Sincronización automática", self._tray_toggle_timer, enabled=configured,
                     toggle=bool(st and st.timer_enabled)),
            MenuItem(separator=True),
            MenuItem("Salir", self.quit_app),
        ]
        sig = [(i.label, i.enabled, i.toggle, i.separator) for i in items]
        if sig != getattr(self, "_menu_sig", None):
            self._menu_sig = sig
            self.tray.set_menu(items)

    def _tray_toggle_timer(self):
        want = not (self.status and self.status.timer_enabled)
        bg(lambda: core.set_timer(want), lambda _: self.refresh(), lambda e: self.notify("Error", str(e)))

    # ------------------------------------------------------------ tareas
    def start_job(self, kind: str, arg: str = ""):
        if self.blocked and kind in ("sync", "resync", "dry-run"):
            self.show_mandatory()
            return
        if self.job:
            self.win.error("Ya hay una tarea en curso")
            return
        try:
            cmd = core.job_command(kind, arg)
        except core.CoreError as e:
            self.win.error(e)
            return
        self.win.append_lines([f"▶ {JOB_TITLES.get(kind, kind)}"], scroll=True)
        self.win.set_job(JOB_TITLES.get(kind, kind))
        if kind in ("dry-run", "add-remote", "install-rclone"):
            self.win.stack.set_visible_child_name("activity")
        # sync/resync: rclone escribe en sync.log, que ya seguimos; evitamos duplicar.
        quiet = kind in ("sync", "resync")
        self.job = core.Job(kind, cmd,
                            on_line=lambda l: None if quiet and not l.startswith(("==>", "[!]", "[x]")) else GLib.idle_add(self.win.append_lines, [l]),
                            on_done=lambda j: GLib.idle_add(self._job_done, j))
        self.refresh()

    def _job_done(self, job: core.Job):
        self.job = None
        self.win.set_job(None)
        ok = job.ok
        title = JOB_TITLES.get(job.kind, job.kind).rstrip("…").split(" (")[0]
        if job.cancelled:
            msg = f"{title}: cancelado"
        else:
            msg = f"{title}: completado" if ok else f"{title}: terminó con errores"
        self.win.append_lines([("✔ " if ok else "✖ ") + msg])
        self.win.toast(msg)
        if not self.win.is_active() and not job.cancelled:
            self.notify(msg, "Abre gdsync para ver los detalles." if not ok else "", urgent=not ok)
        if job.kind == "add-remote":
            self.win.load_settings()
            self.win.load_folder_page()
        if job.kind in ("sync", "resync", "install-rclone"):
            self._quota_remote = None
        self.refresh()

    def cancel_job(self):
        if self.job:
            self.job.cancel()

    # ------------------------------------------------------------ config
    def on_config_saved(self, first_time: bool):
        self._quota_remote = None
        if first_time:
            # Primera vez: activar el timer, sincronizar y arrancar con la sesión.
            def after(_):
                self.set_autostart(True)
                self.win.stack.set_visible_child_name("status")
                self.start_job("sync")
            bg(lambda: core.set_timer(True), after, self.win.error)
        else:
            self.refresh()

    def launch_command(self) -> str:
        """Ruta (sin citar) del ejecutable que relanza esta instalación."""
        if os.environ.get("APPIMAGE"):
            return os.environ["APPIMAGE"]
        if updater.install_kind() == "deb":
            return "/usr/bin/gdsync-desktop"
        launcher = shutil.which("gdsync-desktop", path=core.ENV["PATH"])
        return launcher or str(core.PROJECT_DIR / "gdsync-desktop")

    def set_autostart(self, enabled: bool):
        core.set_autostart(enabled, self.launch_command())

    # ------------------------------------------------------- actualizaciones
    def check_updates(self):
        bg(updater.check, lambda info: info and self._on_update_info(info))
        return True

    def _on_update_info(self, info: updater.UpdateInfo):
        self.update = info
        was_blocked = self.blocked
        self.blocked = info.mandatory
        if info.mandatory:
            self.win.show_update_banner(info, mandatory=True)
            if not was_blocked:
                self.notify("Actualización de seguridad obligatoria",
                            f"La versión {info.current} tiene una vulnerabilidad. Actualiza a {info.latest}.", urgent=True)
                self.show_mandatory()
        elif info.available:
            self.win.show_update_banner(info, mandatory=False)
        else:
            self.win.hide_update_banner()
        self._menu_sig = None
        if self.status:
            self._apply_status(self.status)

    def show_mandatory(self):
        info = self.update
        if not info or self._mandatory_dlg:
            return
        self.show_window()
        body = (f"Tu versión ({info.current}) tiene una vulnerabilidad de seguridad conocida y ya no está soportada. "
                f"La sincronización queda en pausa hasta que actualices a la versión {info.latest}.")
        if info.advisory:
            body += f"\n\n{info.advisory}"
        dlg = Adw.AlertDialog(heading="Actualización de seguridad obligatoria", body=body)
        dlg.set_can_close(False)
        dlg.add_response("quit", "Salir")
        dlg.add_response("details", "Ver detalles")
        dlg.add_response("update", "Actualizar ahora")
        dlg.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("update")

        def resp(_d, r):
            self._mandatory_dlg = None
            if r == "update":
                self.do_update()
            elif r == "details":
                Gtk.UriLauncher.new(info.url).launch(self.win, None, None)
                GLib.timeout_add(300, lambda: self.show_mandatory() and False)
            else:
                self.quit_app()
        dlg.connect("response", resp)
        self._mandatory_dlg = dlg
        dlg.present(self.win)

    def do_update(self):
        info = self.update
        if not info or self._updating:
            return
        if updater.install_kind() == "source":
            Gtk.UriLauncher.new(info.url).launch(self.win, None, None)
            self.win.toast("Instalación desde código fuente: actualiza con «git pull»", timeout=6)
            return
        self._updating = True
        self.win.update_banner_progress(0)

        def progress(f):
            GLib.idle_add(self.win.update_banner_progress, f)

        def done(cmd):
            self._updating = False
            self.notify("gdsync actualizado", f"Versión {info.latest} instalada. Reiniciando…")
            if cmd:
                # Relanzar cuando esta instancia haya salido (instancia única)
                subprocess.Popen(["sh", "-c", 'sleep 1.5; exec "$0" </dev/null >/dev/null 2>&1', cmd],
                                 start_new_session=True)
            self.quit_app()

        def fail(e):
            self._updating = False
            self.win.show_update_banner(info, mandatory=info.mandatory)
            self.win.error(f"No se pudo actualizar: {e}")
            if info.mandatory:
                GLib.timeout_add(500, lambda: self.show_mandatory() and False)
        bg(lambda: updater.apply(info, progress), done, fail)

    def open_folder(self):
        lp = core.read_config()["LOCAL_PATH"]
        if lp:
            self.open_path(lp)

    def open_path(self, path):
        f = Gio.File.new_for_path(str(path))
        Gtk.FileLauncher.new(f).launch(self.win, None, None)

    # -------------------------------------------------------- ajustes app
    def _load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            return {}

    def settings_get(self, key, default=None):
        return self.settings.get(key, default)

    def settings_set(self, key, value):
        self.settings[key] = value
        core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))

    # ------------------------------------------------------- notificación
    def notify(self, title: str, body: str = "", urgent: bool = False):
        if not self.settings.get("notify", True):
            return
        n = Gio.Notification.new(title)
        if body:
            n.set_body(body)
        n.set_icon(Gio.ThemedIcon.new(APP_ID))
        n.set_default_action("app.show")
        if urgent:
            n.set_priority(Gio.NotificationPriority.HIGH)
        self.send_notification(None, n)

    def about(self):
        dlg = Adw.AboutDialog(application_name="Google Drive Sync", application_icon=APP_ID, version=__version__,
                              issue_url=f"https://github.com/{updater.REPO}/issues",
                              developer_name="gdsync", comments="Sincroniza una carpeta de Google Drive con tu equipo.\nBasado en rclone bisync.",
                              website=f"https://github.com/{updater.REPO}", license_type=Gtk.License.MIT_X11)
        dlg.present(self.win)

    def _on_log_lines(self, lines):
        self.win.append_lines(lines)
        # cambios en el log suelen indicar inicio/fin de sync automático
        if any("Bisync successful" in l or "Bisync aborted" in l or "bisync is IN BETA" in l or "Synching Path1" in l for l in lines):
            self.refresh()


def main():
    app = App()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
