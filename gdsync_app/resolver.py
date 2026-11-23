"""Diálogo para revisar y resolver conflictos y duplicados."""
from __future__ import annotations

import difflib
import os
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import conflicts as cf
from . import core
from .window import bg

esc = GLib.markup_escape_text


def _fmt_date(e: cf.Entry) -> str:
    return f"{e.mtime:%d/%m/%Y %H:%M}" if e.mtime else "—"


def _describe(e: cf.Entry) -> str:
    if e.is_dir:
        return f"{e.files} archivo(s) · {core.fmt_bytes(e.size)} · modificado {_fmt_date(e)}"
    return f"{core.fmt_bytes(e.size)} · modificado {_fmt_date(e)}"


def _quoted(name: str) -> str:
    """Muestra espacios al principio/final, que de otro modo no se ven."""
    return f"«{name}»"


class ResolverDialog(Adw.Dialog):
    def __init__(self, app):
        super().__init__(title="Conflictos y duplicados", content_width=820, content_height=660)
        self.app = app
        self.issues: list[cf.Issue] = []
        self.root: Path | None = None
        self._scanning = False

        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_child(self.toasts)

        # -- página de lista
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.spinner = Gtk.Spinner()
        b_ref = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Volver a buscar")
        b_ref.connect("clicked", lambda *_: self.rescan())
        hb.pack_start(b_ref)
        hb.pack_start(self.spinner)
        tv.add_top_bar(hb)
        self.list_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.list_page = Adw.PreferencesPage()
        self.list_groups: list[Adw.PreferencesGroup] = []
        self.list_stack.add_named(self.list_page, "list")
        self.list_stack.add_named(Adw.StatusPage(icon_name="emblem-ok-symbolic", title="Todo en orden",
                                                 description="No hay conflictos ni archivos duplicados."), "empty")
        self.list_stack.add_named(Adw.StatusPage(icon_name="system-search-symbolic", title="Buscando…"), "loading")
        tv.set_content(self.list_stack)
        self.nav.add(Adw.NavigationPage(child=tv, title="Conflictos y duplicados", tag="list"))
        self.rescan()

    # ------------------------------------------------------------- util
    def toast(self, msg: str, timeout: int = 4):
        self.toasts.add_toast(Adw.Toast(title=esc(str(msg)), timeout=timeout))

    def _busy(self) -> bool:
        if self.app.sync_busy():
            self.toast("Espera a que termine la sincronización en curso")
            return True
        return False

    def _rel(self, p: Path) -> str:
        try:
            rel = p.relative_to(self.root).as_posix() if self.root else str(p)
        except ValueError:
            rel = str(p)
        return rel if rel != "." else "Carpeta principal"

    def open_file(self, p: Path):
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(p))).launch(self.get_root(), None, None)

    def open_containing(self, p: Path):
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(p))).open_containing_folder(self.get_root(), None, None)

    def open_drive(self, p: Path | None):
        self.toast("Buscando en Google Drive…", timeout=2)

        def done(url):
            Gtk.UriLauncher.new(url).launch(self.get_root(), None, None)
        bg(lambda: cf.drive_url(p), done, self.toast)

    def copy_drive_link(self, p: Path | None):
        def done(url):
            Gdk.Display.get_default().get_clipboard().set(url)
            self.toast("Enlace de Drive copiado")
        bg(lambda: cf.drive_url(p), done, self.toast)

    def _small_btn(self, icon: str, tip: str, cb) -> Gtk.Button:
        b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER, css_classes=["flat"])
        b.connect("clicked", lambda *_: cb())
        return b

    def _after_action(self, msg: str):
        self.toast(msg + " · se aplicará en Drive en la próxima sincronización", timeout=5)
        self.nav.pop_to_tag("list")
        self.rescan()
        self.app.on_issues_changed()

    def _run(self, fn):
        if self._busy():
            return
        bg(fn, self._after_action, self.toast)

    # ------------------------------------------------------------ lista
    def rescan(self):
        if self._scanning:
            return
        self._scanning = True
        self.spinner.start()
        if not self.issues:
            self.list_stack.set_visible_child_name("loading")

        def work():
            return cf.local_root(), cf.scan()

        def done(res):
            self._scanning = False
            self.spinner.stop()
            self.root, self.issues = res
            self.app.set_issues(self.issues)
            self._fill_list()

        def fail(e):
            self._scanning = False
            self.spinner.stop()
            self.list_stack.set_visible_child_name("empty")
            self.toast(e)
        bg(work, done, fail)

    def _fill_list(self):
        for g in self.list_groups:
            self.list_page.remove(g)
        self.list_groups = []
        if not self.issues:
            self.list_stack.set_visible_child_name("empty")
            return
        self.list_stack.set_visible_child_name("list")
        intro = Adw.PreferencesGroup(description=esc(
            "Los cambios se hacen en tu carpeta local y llegan a Google Drive en la próxima sincronización. "
            "Lo que elimines va a la papelera (aquí y en Drive), así que puedes recuperarlo."))
        self.list_page.add(intro)
        self.list_groups.append(intro)
        conf = [i for i in self.issues if i.kind == "conflict"]
        dups = [i for i in self.issues if i.kind == "duplicate"]
        if conf:
            g = Adw.PreferencesGroup(title="Conflictos", description=esc(
                "El archivo cambió en los dos lados. Se conservó la versión más reciente y la otra se guardó como copia."))
            for i in conf:
                sub = self._rel(i.folder)
                if i.identical:
                    sub += " · contenido idéntico"
                elif i.original_missing:
                    sub += " · el original ya no existe"
                row = Adw.ActionRow(title=esc(i.title), subtitle=esc(sub), activatable=True)
                row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
                row.connect("activated", lambda _r, i=i: self.show_conflict(i))
                g.add(row)
            self.list_page.add(g)
            self.list_groups.append(g)
        if dups:
            g = Adw.PreferencesGroup(title="Duplicados", description=esc(
                "Elementos repetidos con «(1)», «(1) (1)»… o con caracteres que Windows/macOS no admiten."))
            for i in dups:
                n = len(i.entries)
                sub = f"{self._rel(i.folder)} · {n} copias" + (" · idénticas" if i.identical else "")
                row = Adw.ActionRow(title=esc(i.title), subtitle=esc(sub), activatable=True)
                row.add_prefix(Gtk.Image(icon_name="folder-symbolic" if i.entries[0].is_dir else "edit-copy-symbolic"))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
                row.connect("activated", lambda _r, i=i: self.show_duplicate(i))
                g.add(row)
            self.list_page.add(g)
            self.list_groups.append(g)

    # ------------------------------------------------------- vista previa
    def _preview_widget(self, p: Path, height: int = 240) -> Gtk.Widget:
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, height_request=height)
        holder.append(Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True))

        def done(pv):
            child = holder.get_first_child()
            if child:
                holder.remove(child)
            holder.append(self._render_preview(p, pv, height))
        bg(lambda: cf.preview(p), done, lambda e: done({"kind": "error", "msg": str(e)}))
        return holder

    def _render_preview(self, p: Path, pv: dict, height: int) -> Gtk.Widget:
        kind = pv["kind"]
        if kind == "image":
            pic = Gtk.Picture(file=Gio.File.new_for_path(str(p)), content_fit=Gtk.ContentFit.CONTAIN,
                              height_request=height, vexpand=True)
            return pic
        if kind in ("text", "dir"):
            text = pv["text"] if kind == "text" else "\n".join(pv["items"]) or "(carpeta vacía)"
            if kind == "text" and pv.get("truncated"):
                text += "\n…"
            if kind == "dir" and len(pv["items"]) >= 200:
                text += "\n…"
            tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                              css_classes=["logview"])
            tv.get_buffer().set_text(text)
            sw = Gtk.ScrolledWindow(child=tv, min_content_height=height, vexpand=True, css_classes=["card"])
            return sw
        msg = {"missing": "Ya no existe", "binary": "Sin vista previa para este tipo de archivo.\nUsa «Abrir» para verlo."}.get(
            kind, pv.get("msg", "Sin vista previa"))
        return Gtk.Label(label=msg, wrap=True, justify=Gtk.Justification.CENTER, vexpand=True,
                         css_classes=["dim-label"])

    def _diff_widget(self, a: Path, b: Path) -> Gtk.Widget:
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        def work():
            pa, pb = cf.preview(a), cf.preview(b)
            if pa["kind"] != "text" or pb["kind"] != "text":
                return None
            return list(difflib.unified_diff(pa["text"].splitlines(), pb["text"].splitlines(),
                                             "versión actual", "copia en conflicto", lineterm="", n=2))

        def done(lines):
            if lines is None:
                return
            tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                              css_classes=["logview"])
            buf = tv.get_buffer()
            add = buf.create_tag("add", foreground="#26a269")
            rem = buf.create_tag("rem", foreground="#c01c28")
            hunk = buf.create_tag("hunk", foreground="#1c71d8")
            if not lines:
                buf.set_text("Sin diferencias en el texto.")
            for ln in lines[:2000]:
                tag = hunk if ln.startswith("@@") else add if ln.startswith("+") else rem if ln.startswith("-") else None
                it = buf.get_end_iter()
                if tag:
                    buf.insert_with_tags(it, ln + "\n", tag)
                else:
                    buf.insert(it, ln + "\n")
            exp = Gtk.Expander(label="Ver diferencias", margin_top=6)
            exp.set_child(Gtk.ScrolledWindow(child=tv, min_content_height=220, css_classes=["card"], margin_top=6))
            holder.append(exp)
        bg(work, done)
        return holder

    def _file_card(self, title: str, e: cf.Entry, highlight: bool = False) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True,
                      css_classes=["card"], margin_start=4, margin_end=4)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=12,
                        margin_start=12, margin_end=12)
        box.append(inner)
        h = Gtk.Label(label=title, xalign=0, css_classes=["heading"] + (["accent"] if highlight else []))
        name = Gtk.Label(label=_quoted(e.name), xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        meta = Gtk.Label(label=_describe(e) if e.path.exists() else "No existe", xalign=0, css_classes=["caption", "dim-label"])
        for w in (h, name, meta, self._preview_widget(e.path)):
            inner.append(w)
        if e.path.exists():
            btns = Gtk.Box(spacing=4)
            for label, cb in (("Abrir", lambda: self.open_file(e.path)),
                              ("Ver en Drive", lambda: self.open_drive(e.path))):
                b = Gtk.Button(label=label, css_classes=["flat"])
                b.connect("clicked", lambda *_, cb=cb: cb())
                btns.append(b)
            btns.append(self._small_btn("folder-open-symbolic", "Mostrar en la carpeta", lambda: self.open_containing(e.path)))
            btns.append(self._small_btn("edit-copy-symbolic", "Copiar enlace de Drive", lambda: self.copy_drive_link(e.path)))
            inner.append(btns)
        return box

    def _page(self, title: str, body: Gtk.Widget) -> Adw.NavigationPage:
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        clamp = Adw.Clamp(maximum_size=900, child=body, margin_top=12, margin_bottom=18, margin_start=12, margin_end=12)
        tv.set_content(Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER))
        return Adw.NavigationPage(child=tv, title=title)

    def _ask_name(self, heading: str, body: str, initial: str, ok_label: str, cb):
        dlg = Adw.AlertDialog(heading=heading, body=body)
        entry = Gtk.Entry(text=initial, activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("ok", ok_label)
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")
        dlg.connect("response", lambda _d, r: r == "ok" and cb(entry.get_text()))
        dlg.present(self)
        stem, _ext = os.path.splitext(initial)
        GLib.idle_add(lambda: (entry.grab_focus(), entry.select_region(0, len(stem))) and False)

    def _confirm(self, heading: str, body: str, ok_label: str, cb, destructive: bool = False):
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("ok", ok_label)
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda _d, r: r == "ok" and cb())
        dlg.present(self)

    # --------------------------------------------------------- conflicto
    def show_conflict(self, issue: cf.Issue):
        orig, copy = issue.entries
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        where = Gtk.Label(label=f"En: {self._rel(issue.folder)}", xalign=0, wrap=True, css_classes=["dim-label"])
        body.append(where)
        if issue.identical:
            txt = "Las dos versiones tienen exactamente el mismo contenido: puedes quedarte con la actual sin perder nada."
        elif issue.original_missing:
            txt = "El archivo original ya no existe; solo queda la copia en conflicto."
        else:
            txt = ("Este archivo se modificó en tu equipo y en Google Drive a la vez. "
                   "Se mantuvo la versión más reciente con su nombre y la otra se guardó como copia. Elige con cuál quedarte.")
        body.append(Gtk.Label(label=txt, xalign=0, wrap=True))

        flow = Gtk.FlowBox(max_children_per_line=2, min_children_per_line=1, homogeneous=True,
                           selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=8)
        flow.append(self._file_card("Versión actual", orig))
        flow.append(self._file_card("Copia en conflicto", copy))
        body.append(flow)
        if not issue.original_missing and not issue.identical:
            body.append(self._diff_widget(orig.path, copy.path))

        acts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
        row1 = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        if not issue.original_missing:
            b1 = Gtk.Button(label="Quedarme con la actual", css_classes=["pill"] + (["suggested-action"] if issue.identical else []),
                            tooltip_text="La copia en conflicto va a la papelera")
            b1.connect("clicked", lambda *_: self._run(lambda: cf.resolve_conflict(issue, "keep_original")))
            row1.append(b1)
        b2 = Gtk.Button(label="Quedarme con la copia", css_classes=["pill"],
                        tooltip_text="La versión actual va a la papelera y la copia recupera el nombre original")
        b2.connect("clicked", lambda *_: self._run(lambda: cf.resolve_conflict(issue, "keep_copy")))
        row1.append(b2)
        acts.append(row1)
        if not issue.original_missing:
            stem, ext = os.path.splitext(orig.name)
            b3 = Gtk.Button(label="Conservar las dos (renombrar copia)…", css_classes=["pill", "flat"])
            b3.connect("clicked", lambda *_: self._ask_name(
                "Conservar las dos", "Nombre para la copia en conflicto:", f"{stem} (copia){ext}", "Renombrar",
                lambda name: self._run(lambda: cf.resolve_conflict(issue, "keep_both", name))))
            acts.append(b3)
        body.append(acts)
        self.nav.push(self._page(issue.title, body))

    # --------------------------------------------------------- duplicado
    def show_duplicate(self, issue: cf.Issue):
        keeper = cf.suggested_keeper(issue)
        is_dir = issue.entries[0].is_dir
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.append(Gtk.Label(label=f"En: {self._rel(issue.folder)}", xalign=0, wrap=True, css_classes=["dim-label"]))
        why = ("Hay varias copias del mismo elemento. ")
        if any(cf.non_portable(e.name) for e in issue.entries):
            why += ("Alguno de los nombres tiene caracteres que Google Drive para Windows/macOS no admite "
                    "(«:», «?», «*», espacios al final…). Otro equipo lo renombra y Drive acaba creando «(1)», "
                    "«(1) (1)»… en cada ronda. Unifícalos con un nombre sin esos caracteres para cortar el ciclo.")
        else:
            why += "Elige cuál conservar; el resto se une a esa o va a la papelera."
        if issue.identical:
            why += "\n\nTodas las copias tienen el mismo contenido."
        body.append(Gtk.Label(label=why, xalign=0, wrap=True))

        group = Adw.PreferencesGroup(title="Copias", description=esc("Marca la que quieres conservar."))
        first_check = None
        chosen = {"entry": keeper}
        for e in issue.entries:
            row = Adw.ActionRow(title=esc(_quoted(e.name)), subtitle=esc(_describe(e)))
            row.set_title_lines(2)
            check = Gtk.CheckButton(active=e is keeper, valign=Gtk.Align.CENTER)
            if first_check:
                check.set_group(first_check)
            else:
                first_check = check
            check.connect("toggled", lambda c, e=e: c.get_active() and (chosen.__setitem__("entry", e), name_row.set_text(self._clean(e))))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            if cf.non_portable(e.name):
                row.add_suffix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"],
                                         tooltip_text="Nombre no válido en Windows/macOS"))
            row.add_suffix(self._small_btn("view-reveal-symbolic", "Ver contenido", lambda e=e: self.show_preview(e)))
            row.add_suffix(self._small_btn("document-open-symbolic", "Abrir", lambda e=e: self.open_file(e.path)))
            row.add_suffix(self._small_btn("web-browser-symbolic", "Ver en Google Drive", lambda e=e: self.open_drive(e.path)))
            row.add_suffix(self._small_btn("document-edit-symbolic", "Renombrar", lambda e=e: self._rename(e)))
            row.add_suffix(self._small_btn("user-trash-symbolic", "Mover a la papelera", lambda e=e: self._trash(e)))
            group.add(row)
        body.append(group)

        g2 = Adw.PreferencesGroup(title="Unificar")
        name_row = Adw.EntryRow(title="Nombre final")
        name_row.set_text(self._clean(keeper))
        g2.add(name_row)
        body.append(g2)
        expl = ("El contenido de las demás carpetas se mueve a la elegida; los archivos repetidos se descartan "
                "y los que difieren quedan como conflicto para revisarlos." if is_dir else
                "Las copias idénticas van a la papelera; las que difieren quedan como conflicto para revisarlas.")
        body.append(Gtk.Label(label=expl, xalign=0, wrap=True, css_classes=["caption", "dim-label"]))
        b = Gtk.Button(label="Unificar", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)

        def do_merge(*_):
            k, name = chosen["entry"], name_row.get_text().strip()
            n = len(issue.entries) - 1
            self._confirm("¿Unificar copias?",
                          f"Se conservará {_quoted(k.name)}" + (f" con el nombre {_quoted(name)}" if name != k.name else "")
                          + f" y las otras {n} copia(s) se unirán a ella o irán a la papelera.",
                          "Unificar", lambda: self._run(lambda: cf.resolve_duplicates(issue, k.path, name)))
        b.connect("clicked", do_merge)
        body.append(b)
        self.nav.push(self._page(issue.title, body))

    @staticmethod
    def _clean(e: cf.Entry) -> str:
        return cf.clean_name(e)

    def show_preview(self, e: cf.Entry):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.append(Gtk.Label(label=_quoted(e.name), xalign=0, wrap=True, selectable=True, css_classes=["heading"]))
        body.append(Gtk.Label(label=_describe(e), xalign=0, css_classes=["caption", "dim-label"]))
        body.append(self._preview_widget(e.path, height=420))
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        for label, cb in (("Abrir", lambda: self.open_file(e.path)), ("Mostrar en la carpeta", lambda: self.open_containing(e.path)),
                          ("Ver en Google Drive", lambda: self.open_drive(e.path))):
            b = Gtk.Button(label=label, css_classes=["pill"])
            b.connect("clicked", lambda *_, cb=cb: cb())
            btns.append(b)
        body.append(btns)
        self.nav.push(self._page(e.name, body))

    def _rename(self, e: cf.Entry):
        self._ask_name("Renombrar", f"Nuevo nombre para {_quoted(e.name)}:", self._clean(e), "Renombrar",
                       lambda name: self._run(lambda: (cf.rename(e.path, name), f"Renombrado a «{name.strip()}»")[1]))

    def _trash(self, e: cf.Entry):
        self._confirm("¿Mover a la papelera?",
                      f"{_quoted(e.name)} irá a la papelera de este equipo y, tras sincronizar, a la de Google Drive.",
                      "Mover a la papelera",
                      lambda: self._run(lambda: (cf.trash(e.path), f"{_quoted(e.name)} movido a la papelera")[1]),
                      destructive=True)
