"""Ventana principal (GTK4 + libadwaita)."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import core

INTERVALS = [("1min", "Cada minuto"), ("2min", "Cada 2 minutos"), ("5min", "Cada 5 minutos"),
             ("10min", "Cada 10 minutos"), ("15min", "Cada 15 minutos"), ("30min", "Cada 30 minutos"),
             ("1h", "Cada hora"), ("6h", "Cada 6 horas")]

STATE_UI = {
    # estado: (icono, título, clase css)
    "ok": ("gdsync-tray-ok-symbolic", "Todo sincronizado", "success"),
    "syncing": ("gdsync-tray-sync-symbolic", "Sincronizando…", "accent"),
    "error": ("gdsync-tray-error-symbolic", "Hubo un problema", "error"),
    "paused": ("gdsync-tray-paused-symbolic", "Sincronización en pausa", "warning"),
    "setup": ("gdsync-tray-paused-symbolic", "Configura tu sincronización", "dim-label"),
}

CSS = """
.hero-icon { padding: 18px; border-radius: 999px; background: alpha(currentColor, .10); }
.hero-icon.syncing image { animation: gd-spin 1.4s linear infinite; }
@keyframes gd-spin { from { -gtk-icon-transform: rotate(0turn); } to { -gtk-icon-transform: rotate(1turn); } }
.path-chip { padding: 5px 12px; border-radius: 999px; background: alpha(currentColor, .07); }
.logview { font-family: monospace; font-size: 10pt; padding: 12px; }
.filters { font-family: monospace; font-size: 10pt; }
.sync-progress { min-width: 320px; }
"""

CHANGE_UI = {  # (kind, dirección) -> (icono, texto)
    ("new", "up"): ("go-up-symbolic", "Subido"), ("new", "down"): ("go-down-symbolic", "Descargado"),
    ("updated", "up"): ("go-up-symbolic", "Actualizado en Drive"), ("updated", "down"): ("go-down-symbolic", "Actualizado aquí"),
    ("deleted", "up"): ("user-trash-symbolic", "Borrado en Drive"), ("deleted", "down"): ("user-trash-symbolic", "Borrado aquí"),
    ("moved", None): ("document-send-symbolic", "Movido"),
    ("conflict", None): ("dialog-warning-symbolic", "Conflicto: se guardó una copia"),
}


def change_ui(c) -> tuple[str, str]:
    return (CHANGE_UI.get((c.kind, c.direction)) or CHANGE_UI.get((c.kind, None))
            or ({"new": ("document-new-symbolic", "Copiado"), "updated": ("document-edit-symbolic", "Actualizado"),
                 "deleted": ("user-trash-symbolic", "Borrado")}.get(c.kind, ("document-symbolic", c.kind))))


def bg(fn: Callable, done: Callable | None = None, err: Callable | None = None):
    """Ejecuta fn en un hilo y llama done(resultado) / err(exc) en el hilo de GTK."""
    def work():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            if err:
                GLib.idle_add(err, e)
            return
        if done:
            GLib.idle_add(done, res)
    threading.Thread(target=work, daemon=True).start()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Google Drive Sync", default_width=880, default_height=680,
                         width_request=360, height_request=420)
        self.app = app
        self.set_icon_name("io.github.gdsync")
        self.status: core.Status | None = None
        self.browse_remote = ""
        self.browse_path = ""
        self.picked: tuple[str, str] | None = None
        self.local_path = ""
        self.folder_rows: list[Gtk.Widget] = []
        self.remote_rows: list[Gtk.Widget] = []
        self._ls_token = 0
        self._busy = False  # hay un sync en marcha: no se puede tocar config/filtros

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        menu = Gio.Menu()
        menu.append("Abrir carpeta local", "app.open-folder")
        menu.append("Buscar actualizaciones", "app.check-updates")
        menu.append("Acerca de", "app.about")
        menu.append("Salir", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menú"))

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", lambda *_: self.app.do_update())
        view.add_top_bar(self.banner)
        view.set_content(self.stack)
        bar = Adw.ViewSwitcherBar(stack=self.stack)
        view.add_bottom_bar(bar)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # Barra inferior solo en ventanas estrechas
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        bp.add_setter(switcher, "visible", False)
        bp.add_setter(bar, "reveal", True)
        self.add_breakpoint(bp)

        self._build_status()
        self._build_folder()
        self._build_activity()
        self._build_settings()
        self.connect("close-request", self._on_close)

    # ================================================================ util
    def toast(self, msg: str, timeout: int = 3):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=timeout))

    def error(self, e: Exception | str):
        self.toast(str(e), timeout=6)

    def _on_close(self, *_):
        self.set_visible(False)
        self.app.on_window_hidden()
        return True

    def show_update_banner(self, info, mandatory: bool):
        if mandatory:
            self.banner.set_title(GLib.markup_escape_text(f"Actualización de seguridad obligatoria: {info.current} → {info.latest}"))
            self.banner.add_css_class("error")
        else:
            self.banner.set_title(GLib.markup_escape_text(f"Nueva versión disponible: {info.latest}"))
            self.banner.remove_css_class("error")
        self.banner.set_button_label("Actualizar")
        self.banner.set_revealed(True)

    def update_banner_progress(self, frac: float):
        self.banner.set_title(f"Descargando actualización… {int(frac * 100)} %")
        self.banner.set_button_label("")
        self.banner.set_revealed(True)

    def hide_update_banner(self):
        self.banner.set_revealed(False)

    def refresh_update_row(self, info, checking: bool = False, updating: bool = False):
        from . import __version__
        if updating:
            sub, label = f"Versión {__version__} · instalando {info.latest}…", "Actualizando…"
        elif checking:
            sub, label = f"Versión {__version__} · buscando…", "Buscando…"
        elif info and info.available:
            sub, label = f"Versión {__version__} · disponible {info.latest}", f"Actualizar a {info.latest}"
        else:
            sub, label = f"Versión {__version__} · estás al día" if info else f"Versión {__version__}", "Buscar actualizaciones"
        self.row_app_update.set_subtitle(GLib.markup_escape_text(sub))
        self.b_app_update.set_sensitive(not (checking or updating))
        self.b_app_update.set_label(label)
        if info and info.available and not checking and not updating:
            self.b_app_update.add_css_class("suggested-action")
        else:
            self.b_app_update.remove_css_class("suggested-action")

    # ============================================================== ESTADO
    def _build_status(self):
        self.status_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)

        # -- vista configurada
        page = Adw.PreferencesPage()
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER,
                       margin_top=12, margin_bottom=6)
        self.hero_icon_box = Gtk.Box(halign=Gtk.Align.CENTER, css_classes=["hero-icon"])
        self.hero_icon = Gtk.Image(pixel_size=56)
        self.hero_icon_box.append(self.hero_icon)
        self.hero_title = Gtk.Label(css_classes=["title-1"])
        paths = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        def chip(icon):
            box = Gtk.Box(spacing=6, css_classes=["path-chip"])
            box.append(Gtk.Image(icon_name=icon, pixel_size=14))
            lbl = Gtk.Label(css_classes=["caption"], ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=34)
            box.append(lbl)
            return box, lbl
        c1, self.chip_remote = chip("folder-remote-symbolic")
        c2, self.chip_local = chip("computer-symbolic")
        paths.append(c1)
        paths.append(Gtk.Image(icon_name="network-transmit-receive-symbolic", css_classes=["dim-label"], pixel_size=14))
        paths.append(c2)
        buttons = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=10)
        b_open = Gtk.Button(label="Abrir carpeta", css_classes=["pill"])
        b_open.connect("clicked", lambda *_: self.app.open_folder())
        self.b_dry = Gtk.Button(label="Simular", css_classes=["pill"], tooltip_text="Muestra qué cambiaría, sin tocar nada")
        self.b_dry.connect("clicked", lambda *_: self.app.start_job("dry-run"))
        self.b_sync = Gtk.Button(label="Sincronizar ahora", css_classes=["pill", "suggested-action"])
        self.b_sync.connect("clicked", lambda *_: self.app.start_job("sync"))
        for b in (b_open, self.b_dry, self.b_sync):
            buttons.append(b)
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=False,
                                    halign=Gtk.Align.CENTER, css_classes=["sync-progress"])
        self.progress_bar = Gtk.ProgressBar()
        self.progress_label = Gtk.Label(css_classes=["caption"], wrap=True, justify=Gtk.Justification.CENTER)
        self.progress_file = Gtk.Label(css_classes=["caption", "dim-label"], ellipsize=Pango.EllipsizeMode.MIDDLE,
                                       max_width_chars=60)
        for w in (self.progress_bar, self.progress_label, self.progress_file):
            self.progress_box.append(w)
        self.b_stop = Gtk.Button(label="Detener", css_classes=["pill", "destructive-action"], visible=False)
        self.b_stop.connect("clicked", lambda *_: self.app.stop_sync())
        buttons.append(self.b_stop)
        for w in (self.hero_icon_box, self.hero_title, self.progress_box, paths, buttons):
            hero.append(w)
        g0 = Adw.PreferencesGroup()
        g0.add(hero)
        page.add(g0)

        # -- qué pasó y cómo arreglarlo (solo con error)
        self.g_problem = Adw.PreferencesGroup(visible=False)
        self.row_problem = Adw.ActionRow(css_classes=["error"], title_lines=2, subtitle_lines=4)
        self.row_problem.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        self.b_problem = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        self.b_problem.connect("clicked", lambda *_: self._problem_action())
        b_details = Gtk.Button(label="Ver detalles", valign=Gtk.Align.CENTER, css_classes=["flat"])
        b_details.connect("clicked", lambda *_: self.stack.set_visible_child_name("activity"))
        self.row_problem.add_suffix(b_details)
        self.row_problem.add_suffix(self.b_problem)
        self.g_problem.add(self.row_problem)
        page.add(self.g_problem)

        g1 = Adw.PreferencesGroup(title="Sincronización")
        self.row_auto = Adw.SwitchRow(title="Sincronización automática")
        self._auto_handler = self.row_auto.connect("notify::active", self._on_auto_toggled)
        self.row_next = Adw.ActionRow(title="Próxima ejecución", subtitle="—")
        self.row_last = Adw.ActionRow(title="Última sincronización", subtitle="—")
        self.row_conflicts = Adw.ActionRow(title="Archivos con conflicto", subtitle_lines=3, visible=False)
        self.row_conflicts.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
        b_conf = Gtk.Button(label="Ver", valign=Gtk.Align.CENTER)
        b_conf.connect("clicked", lambda *_: self.show_changes())
        self.row_conflicts.add_suffix(b_conf)
        for r in (self.row_auto, self.row_next, self.row_last, self.row_conflicts):
            g1.add(r)
        page.add(g1)

        # Aviso: el client_id compartido de rclone tiene límites y se está retirando
        self.row_client_id = Adw.ActionRow(
            title="Tu cuenta usa el acceso compartido de rclone",
            subtitle="Google lo limita y rclone lo está retirando. Crea tu propio client_id para evitar errores 403 y cortes.",
            subtitle_lines=3, visible=False)
        self.row_client_id.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        b_cid = Gtk.Button(label="Cómo hacerlo", valign=Gtk.Align.CENTER)
        b_cid.connect("clicked", lambda *_: Gtk.UriLauncher.new(core.DRIVE_CLIENT_ID_URL).launch(self, None, None))
        self.row_client_id.add_suffix(b_cid)

        g2 = Adw.PreferencesGroup(title="Almacenamiento")
        self.row_quota = Adw.ActionRow(title="Google Drive", subtitle="Cargando…")
        self.quota_bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=180)
        self.row_quota.add_suffix(self.quota_bar)
        g2.add(self.row_quota)
        g2.add(self.row_client_id)
        page.add(g2)
        self.status_stack.add_named(page, "main")

        # -- vista sin configurar
        empty = Adw.StatusPage(icon_name="io.github.gdsync", title="Bienvenido",
                               description="Elige una carpeta de tu Google Drive para mantenerla sincronizada con este equipo.")
        self.empty_page = empty
        b = Gtk.Button(label="Elegir carpeta", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self.stack.set_visible_child_name("folder"))
        self.b_install = Gtk.Button(label="Instalar rclone", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER, visible=False)
        self.b_install.connect("clicked", lambda *_: self.app.start_job("install-rclone"))
        self.b_choose = b
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(b)
        box.append(self.b_install)
        empty.set_child(box)
        self.status_stack.add_named(empty, "setup")

        self.stack.add_titled_with_icon(self.status_stack, "status", "Estado", "gdsync-tray-ok-symbolic")

    def _on_auto_toggled(self, row, _p):
        want = row.get_active()
        bg(lambda: core.set_timer(want),
           lambda _: (self.toast("Sincronización automática activada" if want else "Sincronización automática en pausa"), self.app.refresh()),
           lambda e: (self.error(e), self._set_auto(not want)))

    def _set_auto(self, value: bool):
        self.row_auto.handler_block(self._auto_handler)
        self.row_auto.set_active(value)
        self.row_auto.handler_unblock(self._auto_handler)

    def update_status(self, st: core.Status, job_running: bool):
        self.status = st
        if job_running != self._busy:
            self._busy = job_running
            self._check_dirty()
            self._filters_check()
        c = st.config
        if not st.rclone or not st.configured:
            self.status_stack.set_visible_child_name("setup")
            self.b_install.set_visible(not st.rclone)
            self.b_choose.set_visible(bool(st.rclone))
            self.empty_page.set_description(
                "rclone no está instalado. Lo instalamos en ~/.local/bin sin permisos de administrador."
                if not st.rclone else
                "Elige una carpeta de tu Google Drive para mantenerla sincronizada con este equipo.")
            return
        self.status_stack.set_visible_child_name("main")
        state = "syncing" if job_running else st.state
        icon, title, cls = STATE_UI[state]
        if state == "paused" and st.timer_enabled and not st.initialized:
            title = "Listo para la primera sincronización"
        self.hero_icon.set_from_icon_name(icon)
        self.hero_icon_box.set_css_classes(["hero-icon", cls] + (["syncing"] if state == "syncing" else []))
        self.hero_title.set_label(title)
        self.chip_remote.set_label(f"{c['REMOTE_NAME']}:{core.remote_label(c['REMOTE_PATH'])}")
        self.chip_local.set_label(c["LOCAL_PATH"].replace(str(core.HOME), "~", 1))
        self.b_sync.set_sensitive(not job_running)
        self.b_dry.set_sensitive(not job_running)
        self._set_auto(st.timer_enabled)
        interval = dict(INTERVALS).get(c["INTERVAL"], c["INTERVAL"]).lower()
        self.row_auto.set_subtitle(interval.capitalize() if st.timer_enabled else "Solo cuando pulses «Sincronizar ahora»")
        self.row_next.set_subtitle(
            (f"{core.fmt_rel(st.next_run)} · {st.next_run:%H:%M}" if st.next_run else "Al iniciar sesión")
            if st.timer_enabled else "—")
        self._show_problem(st.problem if state == "error" else None)
        self.row_client_id.set_visible(st.shared_client_id)
        if st.last_sync:
            self.row_last.set_subtitle(f"{core.fmt_rel(st.last_sync)} · {st.last_sync:%d/%m %H:%M}"
                                       + ("" if st.last_ok else " · con errores, revisa Actividad"))
        else:
            self.row_last.set_subtitle("Nunca")

    def _show_problem(self, d):
        self._problem = d
        self.g_problem.set_visible(d is not None)
        if not d:
            return
        self.row_problem.set_title(GLib.markup_escape_text(d.title))
        self.row_problem.set_subtitle(GLib.markup_escape_text(d.detail))
        self.b_problem.set_visible(bool(d.action))
        self.b_problem.set_label(d.action_label or "Arreglar")
        self.hero_title.set_label(d.title)

    def _problem_action(self):
        d = getattr(self, "_problem", None)
        if not d:
            return
        if d.action == "resync":
            self._confirm_resync()
        elif d.action == "reauth":
            self.app.start_job("reauth", (self.status.config if self.status else core.read_config())["REMOTE_NAME"])
        elif d.action == "open-folder":
            self.app.open_folder()
        elif d.action == "url" and d.url:
            Gtk.UriLauncher.new(d.url).launch(self, None, None)

    def update_quota(self, remote: str, q: dict | None, err: str | None = None):
        self._quota = (remote, q) if q and not err else None
        self.row_quota.set_title(f"Google Drive · {remote}")
        if err or not q:
            self.row_quota.set_subtitle(err or "No disponible")
            return
        used, total = q.get("used", 0), q.get("total")
        if total:
            self.quota_bar.set_value(min(1, used / total))
            self.row_quota.set_subtitle(f"{core.fmt_bytes(used)} de {core.fmt_bytes(total)} usados · {core.fmt_bytes(q.get('free'))} libres")
        else:
            self.row_quota.set_subtitle(f"{core.fmt_bytes(used)} usados")

    # ============================================================= CARPETA
    def _build_folder(self):
        page = Adw.PreferencesPage()

        g_acc = Adw.PreferencesGroup(title="Cuenta")
        self.remote_model = Gtk.StringList()
        self.row_remote = Adw.ComboRow(title="Cuenta de Google Drive", model=self.remote_model)
        self.row_remote.connect("notify::selected", self._on_remote_changed)
        g_acc.add(self.row_remote)
        page.add(g_acc)

        self.g_browse = Adw.PreferencesGroup(title="Carpeta en Google Drive")
        hdr = Gtk.Box(spacing=6)
        self.b_up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Subir", css_classes=["flat"])
        self.b_up.connect("clicked", lambda *_: self._browse(self.browse_remote, "/".join(self.browse_path.split("/")[:-1])))
        self.b_pick = Gtk.Button(label="Usar esta carpeta", css_classes=["suggested-action"])
        self.b_pick.connect("clicked", self._on_pick)
        self.browse_spinner = Gtk.Spinner()
        hdr.append(self.browse_spinner)
        hdr.append(self.b_up)
        hdr.append(self.b_pick)
        self.g_browse.set_header_suffix(hdr)
        page.add(self.g_browse)

        g_dest = Adw.PreferencesGroup(title="En este equipo")
        self.row_picked = Adw.ActionRow(title="Carpeta de Drive elegida", subtitle="Ninguna")
        self.row_picked.add_prefix(Gtk.Image(icon_name="folder-remote-symbolic"))
        self.row_local = Adw.ActionRow(title="Carpeta local", subtitle="—")
        self.row_local.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        b_loc = Gtk.Button(label="Cambiar…", valign=Gtk.Align.CENTER)
        b_loc.connect("clicked", self._on_choose_local)
        self.row_local.add_suffix(b_loc)
        self.row_interval = Adw.ComboRow(title="Frecuencia", model=Gtk.StringList.new([l for _, l in INTERVALS]))
        self.row_interval.connect("notify::selected", lambda *_: self._check_dirty())
        for r in (self.row_picked, self.row_local, self.row_interval):
            g_dest.add(r)
        page.add(g_dest)

        g_save = Adw.PreferencesGroup()
        self.lbl_warn = Gtk.Label(wrap=True, css_classes=["caption", "warning"], visible=False, margin_bottom=8)
        self.b_save = Gtk.Button(label="Guardar cambios", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER, sensitive=False)
        self.b_save.connect("clicked", self._on_save)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.lbl_warn)
        box.append(self.b_save)
        g_save.add(box)
        page.add(g_save)

        self.stack.add_titled_with_icon(page, "folder", "Carpeta", "folder-symbolic")
        self.stack.connect("notify::visible-child-name", self._on_tab)

    def _on_tab(self, *_):
        name = self.stack.get_visible_child_name()
        if name == "folder":
            self.load_folder_page()
        elif name == "settings":
            self.load_settings()

    def load_folder_page(self):
        cfg = core.read_config()
        self.local_path = cfg["LOCAL_PATH"]
        self.picked = (cfg["REMOTE_NAME"], cfg["REMOTE_PATH"]) if cfg["REMOTE_PATH"] else None
        self._refresh_picked()
        keys = [k for k, _ in INTERVALS]
        self.row_interval.set_selected(keys.index(cfg["INTERVAL"]) if cfg["INTERVAL"] in keys else 2)

        def done(remotes):
            names = [r["name"] for r in remotes if r["type"] == "drive"]
            self._remote_names = names
            self.remote_model.splice(0, self.remote_model.get_n_items(), names)
            if not names:
                self._clear_folder_rows()
                self._add_folder_row(Adw.ActionRow(title="No hay cuentas", subtitle="Añade una en Ajustes → Cuentas"))
                return
            sel = names.index(cfg["REMOTE_NAME"]) if cfg["REMOTE_NAME"] in names else 0
            self.row_remote.set_selected(sel)
            parent = "/".join(cfg["REMOTE_PATH"].split("/")[:-1]) if names[sel] == cfg["REMOTE_NAME"] else ""
            self._browse(names[sel], parent)
        bg(core.list_remotes, done, self.error)

    def _on_remote_changed(self, row, _p):
        names = getattr(self, "_remote_names", [])
        i = row.get_selected()
        if 0 <= i < len(names) and names[i] != self.browse_remote:
            self._browse(names[i], "")

    def _clear_folder_rows(self):
        for r in self.folder_rows:
            self.g_browse.remove(r)
        self.folder_rows = []

    def _add_folder_row(self, row):
        self.g_browse.add(row)
        self.folder_rows.append(row)

    def _browse(self, remote: str, path: str):
        self.browse_remote, self.browse_path = remote, path
        self._ls_token += 1
        token = self._ls_token
        crumbs = "Mi unidad" + "".join(f"  ›  {p}" for p in path.split("/") if p)
        self.g_browse.set_description(GLib.markup_escape_text(crumbs))
        self.b_up.set_sensitive(bool(path))
        self.b_pick.set_label("Usar esta carpeta" if path else "Usar todo mi Drive")
        self.browse_spinner.start()

        def done(res):
            if token != self._ls_token:
                return
            self.browse_spinner.stop()
            self._clear_folder_rows()
            for d in res["dirs"]:
                row = Adw.ActionRow(title=GLib.markup_escape_text(d), activatable=True)
                row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
                row.connect("activated", lambda _r, d=d: self._browse(remote, f"{path}/{d}" if path else d))
                self._add_folder_row(row)
            info = f"{len(res['dirs'])} carpetas · {res['files']} archivos en este nivel"
            self._add_folder_row(Adw.ActionRow(title="Sin subcarpetas" if not res["dirs"] else info,
                                               subtitle=info if not res["dirs"] else "", css_classes=["dim-label"]))

        def fail(e):
            if token == self._ls_token:
                self.browse_spinner.stop()
                self._clear_folder_rows()
                self._add_folder_row(Adw.ActionRow(title="No se pudo listar", subtitle=GLib.markup_escape_text(str(e))))
        bg(lambda: core.list_dirs(remote, path), done, fail)

    def _on_pick(self, *_):
        if self.browse_path:
            self._pick(self.browse_path)
            return
        dlg = Adw.AlertDialog(heading="¿Sincronizar todo tu Drive?",
                              body="Se sincronizará toda «Mi unidad» con la carpeta local. "
                                   "Puede ocupar mucho espacio y tardar en la primera sincronización.")
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("ok", "Usar todo mi Drive")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda _d, r: r == "ok" and self._pick(core.ROOT))
        dlg.present(self)

    def _pick(self, rpath: str):
        self.picked = (self.browse_remote, rpath)
        cfg = core.read_config()
        if not self.local_path or (self.local_path == cfg["LOCAL_PATH"] and rpath != cfg["REMOTE_PATH"]):
            sub = "" if rpath == core.ROOT else rpath.split("/")[-1]
            self.local_path = str(core.HOME / "GoogleDrive" / sub)
        self._refresh_picked()

    def _refresh_picked(self):
        self.row_picked.set_subtitle(GLib.markup_escape_text(f"{self.picked[0]}:{core.remote_label(self.picked[1])}") if self.picked else "Ninguna — navega y pulsa «Usar esta carpeta»")
        self.row_local.set_subtitle(GLib.markup_escape_text(self.local_path or "—"))
        self._space_warning = ""
        self._check_dirty()
        if not self.local_path:
            return
        path, picked = self.local_path, self.picked

        def done(free):
            if path != self.local_path or free is None:
                return
            self.row_local.set_subtitle(GLib.markup_escape_text(f"{path} · {core.fmt_bytes(free)} libres"))
            q = getattr(self, "_quota", None)
            # Todo el Drive: avisar si lo que ocupa no cabe en el disco
            if picked and picked[1] == core.ROOT and q and q[0] == picked[0] and q[1].get("used", 0) > free:
                self._space_warning = (f"Tu Drive ocupa {core.fmt_bytes(q[1]['used'])} y en este disco quedan "
                                       f"{core.fmt_bytes(free)} libres: puede que no quepa todo.")
                self._check_dirty()
        bg(lambda: core.free_space(path), done)

    def _on_choose_local(self, *_):
        dlg = Gtk.FileDialog(title="Carpeta local de sincronización", modal=True)
        start = Path(self.local_path) if self.local_path and Path(self.local_path).exists() else core.HOME
        dlg.set_initial_folder(Gio.File.new_for_path(str(start)))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f:
                self.local_path = f.get_path()
                self._refresh_picked()
        dlg.select_folder(self, None, done)

    def _interval(self) -> str:
        return INTERVALS[self.row_interval.get_selected()][0]

    def _check_dirty(self):
        cfg = core.read_config()
        dirty = bool(self.picked) and (
            self.picked != (cfg["REMOTE_NAME"], cfg["REMOTE_PATH"]) or self.local_path != cfg["LOCAL_PATH"]
            or self._interval() != cfg["INTERVAL"])
        target_changed = bool(self.picked) and cfg["REMOTE_PATH"] and (
            self.picked != (cfg["REMOTE_NAME"], cfg["REMOTE_PATH"]) or self.local_path != cfg["LOCAL_PATH"])
        blocked = bool(target_changed) and self._busy
        self.b_save.set_sensitive(dirty and bool(self.local_path) and not blocked)
        msgs = [core.BUSY_MSG if blocked else
                "Al cambiar de carpeta, la próxima sincronización unirá ambos lados. No se borra nada."] if target_changed else []
        if getattr(self, "_space_warning", "") and dirty:
            msgs.append(self._space_warning)
        self.lbl_warn.set_visible(bool(msgs))
        self.lbl_warn.set_label("\n".join(msgs))

    def _on_save(self, *_):
        remote, rpath = self.picked
        first_time = not core.is_configured()

        def done(msg):
            self.toast(msg)
            self._check_dirty()
            self.app.on_config_saved(first_time)
        bg(lambda: core.save_config(remote, rpath, self.local_path, self._interval()), done, self.error)

    # =========================================================== ACTIVIDAD
    def _build_activity(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=10, margin_bottom=10)
        self.act_spinner = Gtk.Spinner()
        self.act_title = Gtk.Label(label="Actividad", xalign=0, hexpand=True, css_classes=["heading"])
        self.act_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        act_switch = Gtk.StackSwitcher(stack=self.act_stack)
        self.b_cancel = Gtk.Button(label="Cancelar", css_classes=["destructive-action"], visible=False)
        self.b_cancel.connect("clicked", lambda *_: self.app.cancel_job())
        b_resync = Gtk.Button(label="Forzar resync", tooltip_text="Une ambos lados desde cero. Úsalo si la sincronización lo pide.")
        b_resync.connect("clicked", self._confirm_resync)
        self.b_resync = b_resync
        b_clear = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Limpiar vista", css_classes=["flat"])
        b_clear.connect("clicked", lambda *_: self.logbuf.set_text(""))
        self.b_clear = b_clear
        self.act_stack.connect("notify::visible-child-name",
                               lambda st, _p: b_clear.set_visible(st.get_visible_child_name() == "log"))
        for w in (self.act_spinner, self.act_title, act_switch, self.b_cancel, b_resync, b_clear):
            bar.append(w)
        box.append(bar)
        box.append(Gtk.Separator())

        self.logbuf = Gtk.TextBuffer()
        for name, color in (("err", "#e01b24"), ("warn", "#c88800"), ("ok", "#26a269"), ("info", "#3584e4"), ("dim", "#8a8a8a")):
            self.logbuf.create_tag(name, foreground=color)
        self.logview = Gtk.TextView(buffer=self.logbuf, editable=False, cursor_visible=False, monospace=True,
                                    wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["logview"])
        self.log_scroll = Gtk.ScrolledWindow(child=self.logview, vexpand=True)

        # Cambios recientes: lista legible (lo más nuevo arriba)
        self.changes_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                                        margin_start=12, margin_end=12, margin_top=12, margin_bottom=12, valign=Gtk.Align.START)
        self.changes_list.set_placeholder(Adw.StatusPage(icon_name="document-open-recent-symbolic", title="Sin cambios recientes",
                                                         description="Aquí verás los archivos que se suban, descarguen o borren."))
        self.changes_list.connect("row-activated", lambda _l, row: self._open_change(row))
        clamp = Adw.Clamp(child=self.changes_list, maximum_size=900)
        self.act_stack.add_titled(Gtk.ScrolledWindow(child=clamp, vexpand=True), "changes", "Cambios")
        self.act_stack.add_titled(self.log_scroll, "log", "Registro")
        b_clear.set_visible(False)
        box.append(self.act_stack)
        self.stack.add_titled_with_icon(box, "activity", "Actividad", "document-open-recent-symbolic")
        history = core.tail(core.LOG_FILE, 300)
        self.append_lines(history or ["Aún no hay actividad. Aquí verás cada sincronización en tiempo real."], scroll=True)

    def append_lines(self, lines: list[str], scroll: bool | None = None):
        if not lines:
            return
        adj = self.log_scroll.get_vadjustment()
        at_bottom = scroll if scroll is not None else adj.get_value() >= adj.get_upper() - adj.get_page_size() - 40
        for line in lines:
            tag = ("err" if any(k in line for k in ("ERROR", "CRITICAL", "aborted", "[x]")) else
                   "warn" if any(k in line for k in ("NOTICE", "WARN", "[!]")) else
                   "ok" if any(k in line for k in ("successful", "Sync OK", "Resync OK", "completada")) else
                   "info" if line.startswith(("==>", "▶")) else None)
            end = self.logbuf.get_end_iter()
            if tag:
                self.logbuf.insert_with_tags_by_name(end, line + "\n", tag)
            else:
                self.logbuf.insert(end, line + "\n")
        # limitar tamaño
        extra = self.logbuf.get_line_count() - 4000
        if extra > 0:
            self.logbuf.delete(self.logbuf.get_start_iter(), self.logbuf.get_iter_at_line(extra)[1])
        if at_bottom:
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)

    def show_changes(self):
        self.stack.set_visible_child_name("activity")
        self.act_stack.set_visible_child_name("changes")

    def update_progress(self, t, syncing: bool):
        self.progress_box.set_visible(syncing)
        self.b_stop.set_visible(syncing)
        if not syncing:
            return
        self.progress_label.set_label(t.summary())
        if t.percent is not None and t.bytes_total and not t.bytes_total.startswith("0 "):
            self.progress_bar.set_fraction(t.percent / 100)
        else:
            self.progress_bar.pulse()
        self.progress_file.set_label(t.current.rsplit("/", 1)[-1] if t.current else "")
        self.progress_file.set_tooltip_text(t.current or None)

    def update_changes(self, t):
        self.changes_list.remove_all()
        for c in reversed(t.changes[-150:]):
            icon, what = change_ui(c)
            name = c.path.rsplit("/", 1)[-1]
            folder = c.path.rsplit("/", 1)[0] if "/" in c.path else ""
            row = Adw.ActionRow(title=GLib.markup_escape_text(name), activatable=c.kind != "deleted",
                                subtitle=GLib.markup_escape_text(" · ".join(x for x in (what, folder, c.when[11:16]) if x)))
            img = Gtk.Image(icon_name=icon)
            if c.kind == "conflict":
                img.add_css_class("warning")
            row.add_prefix(img)
            row._path = c.path
            self.changes_list.append(row)
        conflicts = [c for c in t.changes if c.kind == "conflict"]
        self.row_conflicts.set_visible(bool(conflicts))
        if conflicts:
            n = len(conflicts)
            self.row_conflicts.set_title(f"{n} archivo{'s' if n > 1 else ''} con conflicto")
            self.row_conflicts.set_subtitle("Se editaron en ambos lados. Quedó la versión más nueva y la otra se guardó "
                                            "como copia «.conflictN» junto a ella. Revísalas y borra la que sobre.")

    def _open_change(self, row):
        lp = (self.status.config if self.status else core.read_config()).get("LOCAL_PATH")
        rel = getattr(row, "_path", "")
        if not lp or not rel:
            return
        target = Path(lp) / rel
        self.app.open_path(target.parent if target.parent.exists() else lp)

    def set_job(self, title: str | None):
        running = title is not None
        self.act_spinner.set_spinning(running)
        self.act_title.set_label(title or "Actividad")
        self.b_cancel.set_visible(running)
        self.b_resync.set_sensitive(not running)

    def _confirm_resync(self, *_):
        dlg = Adw.AlertDialog(heading="¿Forzar resync?",
                              body="Compara ambos lados desde cero y une su contenido. No borra archivos, "
                                   "pero en carpetas grandes puede tardar.")
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("go", "Forzar resync")
        dlg.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda _d, r: r == "go" and self.app.start_job("resync"))
        dlg.present(self)

    # ============================================================= AJUSTES
    def _build_settings(self):
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="General")
        self.row_autostart = Adw.SwitchRow(title="Iniciar con la sesión", subtitle="Muestra el icono en la barra superior al entrar")
        self._as_handler = self.row_autostart.connect("notify::active", lambda r, _p: self.app.set_autostart(r.get_active()))
        self.row_notify = Adw.SwitchRow(title="Notificaciones", subtitle="Avisar de errores y sincronizaciones manuales", active=True)
        self.row_notify.connect("notify::active", lambda r, _p: self.app.settings_set("notify", r.get_active()))
        g.add(self.row_autostart)
        g.add(self.row_notify)
        page.add(g)

        self.g_accounts = Adw.PreferencesGroup(title="Cuentas", description="Cuentas de Google Drive conectadas a rclone")
        b_add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Añadir cuenta", css_classes=["flat"])
        b_add.connect("clicked", self._add_account)
        self.g_accounts.set_header_suffix(b_add)
        page.add(self.g_accounts)

        g_f = Adw.PreferencesGroup(title="Exclusiones",
                                   description="Lo excluido no se sube ni se descarga. Lo que ya esté en Drive no se borra.")
        self.b_fs = b_fs = Gtk.Button(label="Guardar", css_classes=["suggested-action"], sensitive=False,
                                      valign=Gtk.Align.CENTER, tooltip_text="Guardar exclusiones")
        b_fs.connect("clicked", self._confirm_save_filters)
        g_f.set_header_suffix(b_fs)
        self.f_presets: set[str] = set()
        self.f_folders: list[str] = []
        self.preset_rows: dict[str, Adw.SwitchRow] = {}
        for key, (title, sub, _rules) in core.FILTER_PRESETS.items():
            row = Adw.SwitchRow(title=title, subtitle=GLib.markup_escape_text(sub))
            row.connect("notify::active", lambda r, _p, k=key: self._on_preset(k, r.get_active()))
            self.preset_rows[key] = row
            g_f.add(row)
        page.add(g_f)

        self.g_ffolders = Adw.PreferencesGroup(title="Carpetas excluidas",
                                               description="Carpetas de tu carpeta sincronizada que no se sincronizan.")
        b_addf = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Excluir una carpeta…", css_classes=["flat"])
        b_addf.connect("clicked", self._add_excluded_folder)
        self.g_ffolders.set_header_suffix(b_addf)
        self.ffolder_rows: list[Gtk.Widget] = []
        page.add(self.g_ffolders)

        g_fadv = Adw.PreferencesGroup()
        exp = Adw.ExpanderRow(title="Reglas avanzadas",
                              subtitle="Una por línea: «- patrón» excluye, «+ patrón» incluye (sintaxis de rclone)")
        self.filters_buf = Gtk.TextBuffer()
        self.filters_buf.connect("changed", lambda *_: self._filters_check())
        tv = Gtk.TextView(buffer=self.filters_buf, monospace=True, top_margin=10, bottom_margin=10,
                          left_margin=12, right_margin=12, css_classes=["filters"])
        exp.add_row(Gtk.ScrolledWindow(child=tv, min_content_height=120, max_content_height=320, propagate_natural_height=True))
        g_fadv.add(exp)
        page.add(g_fadv)

        g_upd = Adw.PreferencesGroup(title="Actualizaciones")
        self.row_app_update = Adw.ActionRow(title="gdsync", subtitle="—")
        self.row_app_update.add_prefix(Gtk.Image(icon_name="software-update-available-symbolic"))
        self.b_app_update = Gtk.Button(label="Buscar actualizaciones", valign=Gtk.Align.CENTER)
        self.b_app_update.connect("clicked", lambda *_: self.app.update_button_clicked())
        self.row_app_update.add_suffix(self.b_app_update)
        g_upd.add(self.row_app_update)
        page.add(g_upd)
        self.refresh_update_row(None)

        g_adv = Adw.PreferencesGroup(title="Avanzado")
        self.row_rclone = Adw.ActionRow(title="rclone", subtitle="—")
        b_up = Gtk.Button(label="Actualizar", valign=Gtk.Align.CENTER)
        b_up.connect("clicked", lambda *_: self.app.start_job("install-rclone"))
        self.row_rclone.add_suffix(b_up)
        row_log = Adw.ActionRow(title="Archivo de registro", subtitle=str(core.LOG_FILE).replace(str(core.HOME), "~"))
        b_log = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        b_log.connect("clicked", lambda *_: self.app.open_path(core.LOG_FILE))
        row_log.add_suffix(b_log)
        row_cfg = Adw.ActionRow(title="Configuración", subtitle=str(core.CONFIG_DIR).replace(str(core.HOME), "~"))
        b_cfg = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        b_cfg.connect("clicked", lambda *_: self.app.open_path(core.CONFIG_DIR))
        row_cfg.add_suffix(b_cfg)
        for r in (self.row_rclone, row_log, row_cfg):
            g_adv.add(r)
        page.add(g_adv)

        self.stack.add_titled_with_icon(page, "settings", "Ajustes", "emblem-system-symbolic")

    def load_settings(self):
        self.row_autostart.handler_block(self._as_handler)
        self.row_autostart.set_active(core.autostart_enabled())
        self.row_autostart.handler_unblock(self._as_handler)
        self.row_notify.set_active(self.app.settings_get("notify", True))
        self._load_filters()
        self.row_rclone.set_subtitle((self.status.rclone if self.status and self.status.rclone else "No instalado"))
        self._load_accounts()

    def _load_accounts(self):
        def done(remotes):
            for r in self.remote_rows:
                self.g_accounts.remove(r)
            self.remote_rows = []
            if not remotes:
                row = Adw.ActionRow(title="Sin cuentas", subtitle="Pulsa + para conectar Google Drive")
                self.g_accounts.add(row)
                self.remote_rows.append(row)
            in_use = core.read_config()
            for rm in remotes:
                row = Adw.ActionRow(title=GLib.markup_escape_text(rm["name"]), subtitle=rm["type"])
                row.add_prefix(Gtk.Image(icon_name="folder-remote-symbolic"))
                if rm["name"] == in_use["REMOTE_NAME"] and in_use["REMOTE_PATH"]:
                    row.add_suffix(Gtk.Label(label="En uso", css_classes=["caption", "accent"], valign=Gtk.Align.CENTER))
                else:
                    b = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="Eliminar")
                    b.connect("clicked", lambda _b, n=rm["name"]: self._delete_account(n))
                    row.add_suffix(b)
                self.g_accounts.add(row)
                self.remote_rows.append(row)
                bg(lambda n=rm["name"]: core.about(n),
                   lambda q, row=row, t=rm["type"]: row.set_subtitle(
                       f"{t} · {core.fmt_bytes(q.get('used'))} de {core.fmt_bytes(q.get('total'))}" if q.get("total") else t))
        bg(core.list_remotes, done, self.error)

    def _add_account(self, *_):
        dlg = Adw.AlertDialog(heading="Conectar Google Drive",
                              body="Ponle un nombre. Se abrirá el navegador para que autorices el acceso.")
        entry = Gtk.Entry(placeholder_text="ej: trabajo", activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("ok", "Conectar")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def resp(_d, r):
            if r == "ok":
                self.app.start_job("add-remote", entry.get_text().strip())
        dlg.connect("response", resp)
        dlg.present(self)

    def _delete_account(self, name: str):
        dlg = Adw.AlertDialog(heading=f"¿Eliminar la cuenta «{name}»?",
                              body="Se borra la conexión de rclone. Tus archivos en Drive y en el equipo no se tocan.")
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("del", "Eliminar")
        dlg.set_response_appearance("del", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "del":
                bg(lambda: core.delete_remote(name), lambda _: (self.toast(f"Cuenta «{name}» eliminada"), self._load_accounts()), self.error)
        dlg.connect("response", resp)
        dlg.present(self)

    # ---------------------------------------------------------- exclusiones
    def _load_filters(self):
        self._saved_filters = core.read_filters() or core.DEFAULT_FILTERS
        presets, folders, custom = core.parse_filters(self._saved_filters)
        self._loading_filters = True
        self.f_presets, self.f_folders = set(presets), list(folders)
        for k, row in self.preset_rows.items():
            row.set_active(k in presets)
        self.filters_buf.set_text("\n".join(custom))
        self._loading_filters = False
        self._render_folder_rows()
        self._filters_check()

    def _filters_text(self) -> str:
        b = self.filters_buf
        custom = [l.strip() for l in b.get_text(b.get_start_iter(), b.get_end_iter(), False).splitlines() if l.strip()]
        return core.build_filters(self.f_presets, self.f_folders, custom)

    def _filters_check(self):
        if getattr(self, "_loading_filters", False) or not hasattr(self, "_saved_filters"):
            return
        dirty = core.parse_filters(self._filters_text()) != core.parse_filters(self._saved_filters)
        self.b_fs.set_sensitive(dirty and not self._busy)
        self.b_fs.set_tooltip_text(core.BUSY_MSG if self._busy and dirty else "Guardar exclusiones")

    def _on_preset(self, key: str, active: bool):
        if getattr(self, "_loading_filters", False):
            return
        (self.f_presets.add if active else self.f_presets.discard)(key)
        self._filters_check()

    def _render_folder_rows(self):
        for r in self.ffolder_rows:
            self.g_ffolders.remove(r)
        self.ffolder_rows = []
        if not self.f_folders:
            row = Adw.ActionRow(title="Ninguna", subtitle="Pulsa + para elegir una carpeta", css_classes=["dim-label"])
            self.g_ffolders.add(row)
            self.ffolder_rows.append(row)
        for f in self.f_folders:
            row = Adw.ActionRow(title=GLib.markup_escape_text(f.rsplit("/", 1)[-1]), subtitle=GLib.markup_escape_text(f))
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            b = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                           tooltip_text="Volver a sincronizar esta carpeta")
            b.connect("clicked", lambda _b, f=f: self._remove_excluded_folder(f))
            row.add_suffix(b)
            self.g_ffolders.add(row)
            self.ffolder_rows.append(row)

    def _remove_excluded_folder(self, f: str):
        if f in self.f_folders:
            self.f_folders.remove(f)
        self._render_folder_rows()
        self._filters_check()

    def _add_excluded_folder(self, *_):
        lp = core.read_config()["LOCAL_PATH"]
        if not lp or not Path(lp).is_dir():
            self.error("Primero configura la carpeta local (y conecta su disco)")
            return
        dlg = Gtk.FileDialog(title="Carpeta a excluir", modal=True, initial_folder=Gio.File.new_for_path(lp))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if not f:
                return
            try:
                rel = core.folder_rule_path(lp, f.get_path())
            except core.CoreError as e:
                self.error(e)
                return
            if rel not in self.f_folders:
                self.f_folders.append(rel)
                self._render_folder_rows()
                self._filters_check()
        dlg.select_folder(self, None, done)

    def _confirm_save_filters(self, *_):
        dlg = Adw.AlertDialog(heading="¿Guardar exclusiones?",
                              body="La próxima sincronización revisará todo desde cero (resync). No se borra nada; "
                                   "lo que ya esté en Drive y ahora quede excluido se mantiene allí.")
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("save", "Guardar")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda _d, r: r == "save" and self._save_filters())
        dlg.present(self)

    def _save_filters(self):
        b = self.filters_buf
        for l in b.get_text(b.get_start_iter(), b.get_end_iter(), False).splitlines():
            l = l.strip()
            if l and not l.startswith("#") and l[:2] not in ("- ", "+ "):
                self.error(f"Regla avanzada inválida: «{l}». Debe empezar por «- » o «+ »")
                return
        text = self._filters_text()

        def done(_):
            self.toast("Exclusiones guardadas · la próxima sincronización hará un resync")
            self._load_filters()
        bg(lambda: core.save_filters(text), done, self.error)
