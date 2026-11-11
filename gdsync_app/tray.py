"""Icono de bandeja sin dependencias extra.

Implementa org.kde.StatusNotifierItem + com.canonical.dbusmenu directamente
sobre Gio D-Bus. Lo muestra la extensión AppIndicator de Ubuntu/GNOME
(y KDE, XFCE, etc. de forma nativa).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from gi.repository import Gio, GLib

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"


@dataclass
class MenuItem:
    label: str = ""
    callback: Callable[[], None] | None = None
    enabled: bool = True
    separator: bool = False
    toggle: bool | None = None  # None = no es checkbox
    icon: str = ""

    def props(self) -> dict:
        if self.separator:
            return {"type": GLib.Variant("s", "separator")}
        p = {"label": GLib.Variant("s", self.label), "enabled": GLib.Variant("b", self.enabled),
             "visible": GLib.Variant("b", True)}
        if self.icon:
            p["icon-name"] = GLib.Variant("s", self.icon)
        if self.toggle is not None:
            p["toggle-type"] = GLib.Variant("s", "checkmark")
            p["toggle-state"] = GLib.Variant("i", 1 if self.toggle else 0)
        return p


class Tray:
    def __init__(self, app_id: str, title: str, icon_theme_path: str, on_activate: Callable[[], None]):
        self.app_id, self.title = app_id, title
        self.icon_theme_path = icon_theme_path
        self.on_activate = on_activate
        self.icon = "gdsync-tray-ok-symbolic"
        self.tooltip = title
        self.items: list[MenuItem] = []
        self.revision = 1
        self.conn: Gio.DBusConnection | None = None
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        self._menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        Gio.bus_own_name(Gio.BusType.SESSION, self.bus_name, Gio.BusNameOwnerFlags.NONE,
                         self._on_bus, None, None)

    # ----------------------------------------------------------- público
    def set_menu(self, items: list[MenuItem]) -> None:
        self.items = items
        self.revision += 1
        if self.conn:
            self.conn.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                                  GLib.Variant("(ui)", (self.revision, 0)))

    def set_icon(self, icon: str, tooltip: str) -> None:
        changed_icon = icon != self.icon
        self.icon, self.tooltip = icon, tooltip
        if self.conn:
            if changed_icon:
                self._emit("NewIcon")
            self._emit("NewToolTip")

    # ----------------------------------------------------------- D-Bus
    def _on_bus(self, conn, _name):
        self.conn = conn
        conn.register_object(SNI_PATH, self._sni_info, self._sni_call, self._sni_get, None)
        conn.register_object(MENU_PATH, self._menu_info, self._menu_call, self._menu_get, None)
        self._register_watcher()
        # Si el watcher (la extensión) se reinicia, volver a registrarse.
        Gio.bus_watch_name(Gio.BusType.SESSION, "org.kde.StatusNotifierWatcher",
                           Gio.BusNameWatcherFlags.NONE, lambda *_: self._register_watcher(), None)

    def _register_watcher(self):
        if not self.conn:
            return
        self.conn.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                       "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                       GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                       lambda c, res: self._finish(c, res))

    @staticmethod
    def _finish(conn, res):
        try:
            conn.call_finish(res)
        except GLib.Error as e:
            print(f"[tray] Sin StatusNotifierWatcher (¿extensión AppIndicator desactivada?): {e.message}")

    def _emit(self, signal: str):
        self.conn.emit_signal(None, SNI_PATH, "org.kde.StatusNotifierItem", signal, None)

    def _sni_get(self, _c, _s, _p, _i, prop):
        vals = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon),
            "IconThemePath": GLib.Variant("s", self.icon_theme_path),
            "AttentionIconName": GLib.Variant("s", ""),
            "OverlayIconName": GLib.Variant("s", ""),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon, [], self.title, self.tooltip)),
        }
        return vals.get(prop)

    def _sni_call(self, _c, _s, _p, _i, method, _params, inv):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self.on_activate)
        inv.return_value(None)

    # ----------------------------------------------------------- menú
    def _node(self, idx: int) -> GLib.Variant:
        item = self.items[idx - 1]
        return GLib.Variant("(ia{sv}av)", (idx, item.props(), []))

    def _layout(self):
        children = [self._node(i) for i in range(1, len(self.items) + 1)]
        root_props = {"children-display": GLib.Variant("s", "submenu")}
        return GLib.Variant("(u(ia{sv}av))", (self.revision, (0, root_props, children)))

    def _menu_get(self, _c, _s, _p, _i, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [self.icon_theme_path]),
        }.get(prop)

    def _menu_call(self, _c, _s, _p, _i, method, params, inv):
        if method == "GetLayout":
            inv.return_value(self._layout())
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            out = [(i, self.items[i - 1].props()) for i in ids if 0 < i <= len(self.items)]
            if 0 in ids:
                out.insert(0, (0, {"children-display": GLib.Variant("s", "submenu")}))
            inv.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            i, name = params.unpack()
            props = self.items[i - 1].props() if 0 < i <= len(self.items) else {}
            inv.return_value(GLib.Variant("(v)", (props.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            i, event, _data, _ts = params.unpack()
            if event == "clicked":
                self._click(i)
            inv.return_value(None)
        elif method == "EventGroup":
            for i, event, _d, _t in params.unpack()[0]:
                if event == "clicked":
                    self._click(i)
            inv.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _click(self, i: int):
        if 0 < i <= len(self.items) and self.items[i - 1].callback and self.items[i - 1].enabled:
            GLib.idle_add(self.items[i - 1].callback)
