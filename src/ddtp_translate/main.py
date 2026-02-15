#!/usr/bin/env python3
"""DDTP Translate — GTK4/Adwaita app for translating Debian package descriptions."""

import gettext
import locale
import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import __version__
from .ddtp_api import DDTP_LANGUAGES, fetch_untranslated
from .smtp_sender import load_settings, save_settings, send_translation

# i18n setup
LOCALE_DIR = None
for d in [
    os.path.join(os.path.dirname(__file__), "..", "..", "po"),
    "/usr/share/locale",
    "/usr/local/share/locale",
]:
    if os.path.isdir(d):
        LOCALE_DIR = d
        break

gettext.bindtextdomain("ddtp-translate", LOCALE_DIR)
gettext.textdomain("ddtp-translate")
_ = gettext.gettext

APP_ID = "se.danielnylander.ddtp-translate"


class PreferencesWindow(Adw.PreferencesWindow):
    """SMTP and app settings."""

    def __init__(self, parent, **kwargs):
        super().__init__(
            title=_("Preferences"),
            transient_for=parent,
            **kwargs,
        )
        self.settings = load_settings()

        # SMTP page
        smtp_page = Adw.PreferencesPage(title=_("SMTP"), icon_name="mail-send-symbolic")
        smtp_group = Adw.PreferencesGroup(title=_("Mail Server"))

        self.host_row = Adw.EntryRow(title=_("SMTP Host"))
        self.host_row.set_text(self.settings.get("smtp_host", ""))
        smtp_group.add(self.host_row)

        self.port_row = Adw.EntryRow(title=_("SMTP Port"))
        self.port_row.set_text(str(self.settings.get("smtp_port", 25)))
        smtp_group.add(self.port_row)

        self.user_row = Adw.EntryRow(title=_("Username"))
        self.user_row.set_text(self.settings.get("smtp_user", ""))
        smtp_group.add(self.user_row)

        self.pass_row = Adw.PasswordEntryRow(title=_("Password"))
        self.pass_row.set_text(self.settings.get("smtp_password", ""))
        smtp_group.add(self.pass_row)

        self.tls_row = Adw.SwitchRow(title=_("Use TLS"))
        self.tls_row.set_active(self.settings.get("smtp_use_tls", False))
        smtp_group.add(self.tls_row)

        smtp_page.add(smtp_group)

        # Identity
        id_group = Adw.PreferencesGroup(title=_("Identity"))
        self.name_row = Adw.EntryRow(title=_("Your Name"))
        self.name_row.set_text(self.settings.get("from_name", ""))
        id_group.add(self.name_row)

        self.email_row = Adw.EntryRow(title=_("Your Email"))
        self.email_row.set_text(self.settings.get("from_email", ""))
        id_group.add(self.email_row)
        smtp_page.add(id_group)

        self.add(smtp_page)

        self.connect("close-request", self._on_close)

    def _on_close(self, *_args):
        self.settings.update(
            {
                "smtp_host": self.host_row.get_text(),
                "smtp_port": int(self.port_row.get_text() or 25),
                "smtp_user": self.user_row.get_text(),
                "smtp_password": self.pass_row.get_text(),
                "smtp_use_tls": self.tls_row.get_active(),
                "from_name": self.name_row.get_text(),
                "from_email": self.email_row.get_text(),
            }
        )
        save_settings(self.settings)
        return False


class MainWindow(Adw.ApplicationWindow):
    """Main application window."""

    def __init__(self, app, **kwargs):
        super().__init__(application=app, title=_("DDTP Translate"), default_width=1100, default_height=700, **kwargs)

        self.packages = []
        self.current_pkg = None
        self.settings = load_settings()

        # Main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)

        # Header bar
        header = Adw.HeaderBar()
        main_box.append(header)

        # Language dropdown
        lang_store = Gtk.StringList()
        self._lang_codes = []
        for code, name in DDTP_LANGUAGES:
            lang_store.append(f"{name} ({code})")
            self._lang_codes.append(code)

        self.lang_dropdown = Gtk.DropDown(model=lang_store)
        # Set default language
        default_lang = self.settings.get("default_language", "sv")
        if default_lang in self._lang_codes:
            self.lang_dropdown.set_selected(self._lang_codes.index(default_lang))
        self.lang_dropdown.connect("notify::selected", self._on_lang_changed)
        header.pack_start(self.lang_dropdown)

        # Refresh button
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh_btn.connect("clicked", self._on_refresh)
        header.pack_start(refresh_btn)

        # Stats label
        self.stats_label = Gtk.Label(label="")
        self.stats_label.add_css_class("dim-label")
        header.pack_start(self.stats_label)

        # Hamburger menu
        menu = Gio.Menu()
        menu.append(_("Preferences"), "app.preferences")
        menu.append(_("About"), "app.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_btn)

        # Content: sidebar + editor
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(280)
        paned.set_vexpand(True)
        main_box.append(paned)

        # Sidebar
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.set_size_request(250, -1)

        # Search
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Filter packages…"))
        self.search_entry.set_margin_start(6)
        self.search_entry.set_margin_end(6)
        self.search_entry.set_margin_top(6)
        self.search_entry.set_margin_bottom(6)
        self.search_entry.connect("search-changed", self._on_search_changed)
        sidebar_box.append(self.search_entry)

        # Package list
        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.pkg_list = Gtk.ListBox()
        self.pkg_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.pkg_list.connect("row-selected", self._on_pkg_selected)
        scroll.set_child(self.pkg_list)
        sidebar_box.append(scroll)

        paned.set_start_child(sidebar_box)

        # Editor area
        editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Side-by-side pane
        editor_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        editor_paned.set_vexpand(True)

        # Left: original
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left_label = Gtk.Label(label=_("Original (English)"), xalign=0)
        left_label.add_css_class("heading")
        left_label.set_margin_start(8)
        left_label.set_margin_top(6)
        left_box.append(left_label)

        left_scroll = Gtk.ScrolledWindow(vexpand=True)
        self.orig_view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD)
        self.orig_view.set_margin_start(8)
        self.orig_view.set_margin_end(4)
        self.orig_view.set_margin_top(4)
        self.orig_view.set_margin_bottom(4)
        left_scroll.set_child(self.orig_view)
        left_box.append(left_scroll)
        editor_paned.set_start_child(left_box)

        # Right: translation
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right_label = Gtk.Label(label=_("Translation"), xalign=0)
        right_label.add_css_class("heading")
        right_label.set_margin_start(8)
        right_label.set_margin_top(6)
        right_box.append(right_label)

        right_scroll = Gtk.ScrolledWindow(vexpand=True)
        self.trans_view = Gtk.TextView(editable=True, wrap_mode=Gtk.WrapMode.WORD)
        self.trans_view.set_margin_start(4)
        self.trans_view.set_margin_end(8)
        self.trans_view.set_margin_top(4)
        self.trans_view.set_margin_bottom(4)
        right_scroll.set_child(self.trans_view)
        right_box.append(right_scroll)
        editor_paned.set_end_child(right_box)

        editor_box.append(editor_paned)

        # Bottom bar
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.set_margin_start(8)
        bottom.set_margin_end(8)
        bottom.set_margin_top(6)
        bottom.set_margin_bottom(6)

        self.status_label = Gtk.Label(label=_("Ready"), xalign=0, hexpand=True)
        self.status_label.add_css_class("dim-label")
        bottom.append(self.status_label)

        self.submit_btn = Gtk.Button(label=_("Submit Translation"))
        self.submit_btn.add_css_class("suggested-action")
        self.submit_btn.set_sensitive(False)
        self.submit_btn.connect("clicked", self._on_submit)
        bottom.append(self.submit_btn)

        editor_box.append(bottom)
        paned.set_end_child(editor_box)

        # Load initial data
        self._refresh_packages()

    def _current_lang(self):
        idx = self.lang_dropdown.get_selected()
        if 0 <= idx < len(self._lang_codes):
            return self._lang_codes[idx]
        return "sv"

    def _on_lang_changed(self, *_args):
        self._refresh_packages()

    def _on_refresh(self, *_args):
        self._refresh_packages(force=True)

    def _refresh_packages(self, force=False):
        lang = self._current_lang()
        self.status_label.set_text(_("Loading…"))
        self.pkg_list.remove_all() if hasattr(self.pkg_list, "remove_all") else self._clear_list()

        def do_fetch():
            try:
                pkgs = fetch_untranslated(lang, force_refresh=force)
                GLib.idle_add(self._populate_list, pkgs)
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text, str(exc))

        threading.Thread(target=do_fetch, daemon=True).start()

    def _clear_list(self):
        while True:
            row = self.pkg_list.get_row_at_index(0)
            if row is None:
                break
            self.pkg_list.remove(row)

    def _populate_list(self, pkgs):
        self.packages = pkgs
        self._clear_list()
        for pkg in pkgs:
            label = Gtk.Label(label=pkg["package"], xalign=0)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            self.pkg_list.append(label)
        self.stats_label.set_text(_("{n} untranslated").format(n=len(pkgs)))
        self.status_label.set_text(_("Ready"))

    def _on_search_changed(self, entry):
        query = entry.get_text().lower()
        idx = 0
        while True:
            row = self.pkg_list.get_row_at_index(idx)
            if row is None:
                break
            child = row.get_child()
            visible = query in child.get_text().lower() if child else True
            row.set_visible(visible)
            idx += 1

    def _on_pkg_selected(self, _listbox, row):
        if row is None:
            self.current_pkg = None
            self.submit_btn.set_sensitive(False)
            return

        idx = row.get_index()
        if 0 <= idx < len(self.packages):
            pkg = self.packages[idx]
            self.current_pkg = pkg
            desc = pkg["short"]
            if pkg["long"]:
                desc += "\n\n" + pkg["long"]
            self.orig_view.get_buffer().set_text(desc)
            self.trans_view.get_buffer().set_text("")
            self.submit_btn.set_sensitive(True)
            self.status_label.set_text(_("Editing: {pkg}").format(pkg=pkg["package"]))

    def _on_submit(self, *_args):
        if not self.current_pkg:
            return

        buf = self.trans_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text:
            self.status_label.set_text(_("Translation is empty"))
            return

        lines = text.split("\n", 1)
        short = lines[0]
        long_text = lines[1].strip() if len(lines) > 1 else ""

        pkg = self.current_pkg
        lang = self._current_lang()
        self.submit_btn.set_sensitive(False)
        self.status_label.set_text(_("Sending…"))

        def do_send():
            try:
                send_translation(pkg["package"], pkg["md5"], lang, short, long_text)
                GLib.idle_add(self.status_label.set_text, _("Sent successfully!"))
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text, _("Error: {e}").format(e=str(exc)))
            GLib.idle_add(self.submit_btn.set_sensitive, True)

        threading.Thread(target=do_send, daemon=True).start()


class DDTPTranslateApp(Adw.Application):
    """Main application."""

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

        self.create_action("preferences", self._on_preferences)
        self.create_action("about", self._on_about)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = MainWindow(self)
        win.present()

    def create_action(self, name, callback):
        action = Gio.SimpleAction(name=name)
        action.connect("activate", callback)
        self.add_action(action)

    def _on_preferences(self, *_args):
        win = PreferencesWindow(self.props.active_window)
        win.present()

    def _on_about(self, *_args):
        about = Adw.AboutWindow(
            transient_for=self.props.active_window,
            application_name=_("DDTP Translate"),
            application_icon=APP_ID,
            version=__version__,
            developer_name="Daniel Nylander",
            developers=["Daniel Nylander <daniel@danielnylander.se>"],
            copyright="© 2025 Daniel Nylander",
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/yeager/ddtp-translate",
            issue_url="https://github.com/yeager/ddtp-translate/issues",
            comments=_("Translate Debian package descriptions via DDTP"),
        )
        about.present()


def main():
    app = DDTPTranslateApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
