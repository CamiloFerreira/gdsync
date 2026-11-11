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
"""


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
        for w in (self.hero_icon_box, self.hero_title, paths, buttons):
            hero.append(w)
        g0 = Adw.PreferencesGroup()
        g0.add(hero)
        page.add(g0)

        g1 = Adw.PreferencesGroup(title="Sincronización")
        self.row_auto = Adw.SwitchRow(title="Sincronización automática")
        self._auto_handler = self.row_auto.connect("notify::active", self._on_auto_toggled)
        self.row_next = Adw.ActionRow(title="Próxima ejecución", subtitle="—")
        self.row_last = Adw.ActionRow(title="Última sincronización", subtitle="—")
        for r in (self.row_auto, self.row_next, self.row_last):
            g1.add(r)
        page.add(g1)

        g2 = Adw.PreferencesGroup(title="Almacenamiento")
        self.row_quota = Adw.ActionRow(title="Google Drive", subtitle="Cargando…")
        self.quota_bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=180)
        self.row_quota.add_suffix(self.quota_bar)
        g2.add(self.row_quota)
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
        self.chip_remote.set_label(f"{c['REMOTE_NAME']}:{c['REMOTE_PATH']}")
        self.chip_local.set_label(c["LOCAL_PATH"].replace(str(core.HOME), "~", 1))
        self.b_sync.set_sensitive(not job_running)
        self.b_dry.set_sensitive(not job_running)
        self._set_auto(st.timer_enabled)
        interval = dict(INTERVALS).get(c["INTERVAL"], c["INTERVAL"]).lower()
        self.row_auto.set_subtitle(interval.capitalize() if st.timer_enabled else "Solo cuando pulses «Sincronizar ahora»")
        self.row_next.set_subtitle(
            (f"{core.fmt_rel(st.next_run)} · {st.next_run:%H:%M}" if st.next_run else "Al iniciar sesión")
            if st.timer_enabled else "—")
        if st.last_sync:
            self.row_last.set_subtitle(f"{core.fmt_rel(st.last_sync)} · {st.last_sync:%d/%m %H:%M}"
                                       + ("" if st.last_ok else " · con errores, revisa Actividad"))
        else:
            self.row_last.set_subtitle("Nunca")

    def update_quota(self, remote: str, q: dict | None, err: str | None = None):
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
        self.b_pick.set_sensitive(bool(path))
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
        self.picked = (self.browse_remote, self.browse_path)
        cfg = core.read_config()
        if not self.local_path or (self.local_path == cfg["LOCAL_PATH"] and self.browse_path != cfg["REMOTE_PATH"]):
            self.local_path = str(core.HOME / "GoogleDrive" / self.browse_path.split("/")[-1])
        self._refresh_picked()

    def _refresh_picked(self):
        self.row_picked.set_subtitle(GLib.markup_escape_text(f"{self.picked[0]}:{self.picked[1]}") if self.picked else "Ninguna — navega y pulsa «Usar esta carpeta»")
        self.row_local.set_subtitle(GLib.markup_escape_text(self.local_path or "—"))
        self._check_dirty()

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
        self.b_save.set_sensitive(dirty and bool(self.local_path))
        target_changed = bool(self.picked) and cfg["REMOTE_PATH"] and (
            self.picked != (cfg["REMOTE_NAME"], cfg["REMOTE_PATH"]) or self.local_path != cfg["LOCAL_PATH"])
        self.lbl_warn.set_visible(bool(target_changed))
        self.lbl_warn.set_label("Al cambiar de carpeta, la próxima sincronización unirá ambos lados. No se borra nada.")

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
        self.act_title = Gtk.Label(label="Registro de sincronización", xalign=0, hexpand=True, css_classes=["heading"])
        self.b_cancel = Gtk.Button(label="Cancelar", css_classes=["destructive-action"], visible=False)
        self.b_cancel.connect("clicked", lambda *_: self.app.cancel_job())
        b_resync = Gtk.Button(label="Forzar resync", tooltip_text="Une ambos lados desde cero. Úsalo si la sincronización lo pide.")
        b_resync.connect("clicked", self._confirm_resync)
        self.b_resync = b_resync
        b_clear = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Limpiar vista", css_classes=["flat"])
        b_clear.connect("clicked", lambda *_: self.logbuf.set_text(""))
        for w in (self.act_spinner, self.act_title, self.b_cancel, b_resync, b_clear):
            bar.append(w)
        box.append(bar)
        box.append(Gtk.Separator())

        self.logbuf = Gtk.TextBuffer()
        for name, color in (("err", "#e01b24"), ("warn", "#c88800"), ("ok", "#26a269"), ("info", "#3584e4"), ("dim", "#8a8a8a")):
            self.logbuf.create_tag(name, foreground=color)
        self.logview = Gtk.TextView(buffer=self.logbuf, editable=False, cursor_visible=False, monospace=True,
                                    wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["logview"])
        self.log_scroll = Gtk.ScrolledWindow(child=self.logview, vexpand=True)
        box.append(self.log_scroll)
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

    def set_job(self, title: str | None):
        running = title is not None
        self.act_spinner.set_spinning(running)
        self.act_title.set_label(title or "Registro de sincronización")
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
                                   description="Una regla por línea: «- patrón» excluye, «+ patrón» incluye, «#» comenta.")
        b_fs = Gtk.Button(label="Guardar", css_classes=["flat"])
        b_fs.connect("clicked", self._save_filters)
        g_f.set_header_suffix(b_fs)
        self.filters_buf = Gtk.TextBuffer()
        tv = Gtk.TextView(buffer=self.filters_buf, monospace=True, top_margin=10, bottom_margin=10,
                          left_margin=12, right_margin=12, css_classes=["filters"])
        frame = Gtk.Frame(child=Gtk.ScrolledWindow(child=tv, min_content_height=180, max_content_height=320, propagate_natural_height=True),
                          css_classes=["card"])
        g_f.add(frame)
        page.add(g_f)

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
        self.filters_buf.set_text(core.read_filters() or core.DEFAULT_FILTERS)
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

    def _save_filters(self, *_):
        b = self.filters_buf
        text = b.get_text(b.get_start_iter(), b.get_end_iter(), False)
        try:
            core.save_filters(text)
            self.toast("Exclusiones guardadas · la próxima sincronización hará un resync")
        except core.CoreError as e:
            self.error(e)
