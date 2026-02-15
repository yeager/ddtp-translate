#!/usr/bin/env python3
"""DDTP Translate — GTK4/Adwaita app for translating Debian package descriptions."""

import gettext
import locale
import os
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Gdk, Pango  # noqa: E402

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

# Default email delay between submissions (seconds)
DEFAULT_SEND_DELAY = 30


def _setup_heatmap_css():
    css = b"""
    .heatmap-green { background-color: #26a269; color: white; border-radius: 8px; }
    .heatmap-red { background-color: #c01c28; color: white; border-radius: 8px; }
    .heatmap-gray { background-color: #77767b; color: white; border-radius: 8px; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


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

        # Preset buttons
        preset_group = Adw.PreferencesGroup(title=_("Quick Setup"))

        gmail_btn = Gtk.Button(label=_("Use Gmail"))
        gmail_btn.add_css_class("suggested-action")
        gmail_btn.add_css_class("pill")
        gmail_btn.set_margin_top(4)
        gmail_btn.set_margin_bottom(4)
        gmail_btn.connect("clicked", self._apply_gmail_preset)
        preset_group.add(gmail_btn)

        smtp_page.add(smtp_group)
        smtp_page.add(preset_group)

        # Identity
        id_group = Adw.PreferencesGroup(title=_("Identity"))
        self.name_row = Adw.EntryRow(title=_("Your Name"))
        self.name_row.set_text(self.settings.get("from_name", ""))
        id_group.add(self.name_row)

        self.email_row = Adw.EntryRow(title=_("Your Email"))
        self.email_row.set_text(self.settings.get("from_email", ""))
        id_group.add(self.email_row)
        smtp_page.add(id_group)

        # Sending page
        send_page = Adw.PreferencesPage(title=_("Sending"), icon_name="preferences-system-time-symbolic")
        send_group = Adw.PreferencesGroup(
            title=_("Rate Limiting"),
            description=_("Delay between email submissions to avoid flooding the DDTP server."),
        )

        self.delay_row = Adw.SpinRow.new_with_range(0, 300, 5)
        self.delay_row.set_title(_("Delay between emails (seconds)"))
        self.delay_row.set_value(self.settings.get("send_delay", DEFAULT_SEND_DELAY))
        send_group.add(self.delay_row)

        send_page.add(send_group)
        self.add(send_page)

        self.add(smtp_page)

        self.connect("close-request", self._on_close)

    def _apply_gmail_preset(self, _btn):
        self.host_row.set_text("smtp.gmail.com")
        self.port_row.set_text("465")
        self.tls_row.set_active(True)

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
                "send_delay": int(self.delay_row.get_value()),
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
        self._heatmap_mode = False
        self._sort_ascending = True
        self._last_send_time = 0

        _setup_heatmap_css()

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

        # Sort button
        sort_btn = Gtk.Button(icon_name="view-sort-ascending-symbolic", tooltip_text=_("Sort packages"))
        sort_btn.connect("clicked", self._on_sort_clicked)
        header.pack_start(sort_btn)
        self._sort_btn = sort_btn

        # Heatmap toggle
        self._heatmap_btn = Gtk.ToggleButton(icon_name="view-grid-symbolic")
        self._heatmap_btn.set_tooltip_text(_("Toggle heatmap view"))
        self._heatmap_btn.connect("toggled", self._on_heatmap_toggled)
        header.pack_start(self._heatmap_btn)

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

        # Progress bar (hidden by default)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_margin_start(6)
        self._progress_bar.set_margin_end(6)
        self._progress_bar.set_visible(False)
        sidebar_box.append(self._progress_bar)

        # Package list
        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.pkg_list = Gtk.ListBox()
        self.pkg_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.pkg_list.connect("row-selected", self._on_pkg_selected)
        scroll.set_child(self.pkg_list)

        # Heatmap view for sidebar
        hm_scroll = Gtk.ScrolledWindow(vexpand=True)
        self._heatmap_flow = Gtk.FlowBox()
        self._heatmap_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._heatmap_flow.set_homogeneous(True)
        self._heatmap_flow.set_min_children_per_line(2)
        self._heatmap_flow.set_max_children_per_line(4)
        self._heatmap_flow.set_column_spacing(4)
        self._heatmap_flow.set_row_spacing(4)
        self._heatmap_flow.set_margin_start(6)
        self._heatmap_flow.set_margin_end(6)
        self._heatmap_flow.set_margin_top(6)
        self._heatmap_flow.set_margin_bottom(6)
        hm_scroll.set_child(self._heatmap_flow)

        self._sidebar_stack = Gtk.Stack()
        self._sidebar_stack.add_named(scroll, "list")
        self._sidebar_stack.add_named(hm_scroll, "heatmap")
        sidebar_box.append(self._sidebar_stack)

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

    def _on_sort_clicked(self, btn):
        self._sort_ascending = not self._sort_ascending
        btn.set_icon_name(
            "view-sort-ascending-symbolic" if self._sort_ascending else "view-sort-descending-symbolic"
        )
        self.packages.sort(key=lambda p: p.get("package", "").lower(), reverse=not self._sort_ascending)
        self._populate_list(self.packages)

    def _refresh_packages(self, force=False):
        lang = self._current_lang()
        self.status_label.set_text(_("Loading…"))
        self._progress_bar.set_visible(True)
        self._progress_bar.set_fraction(0.0)
        self._progress_bar.set_text(_("Downloading package data…"))
        self._progress_bar.set_show_text(True)
        self._clear_list()

        # Pulse the progress bar while loading
        self._loading = True

        def pulse():
            if self._loading:
                self._progress_bar.pulse()
                return True
            return False

        GLib.timeout_add(150, pulse)

        def do_fetch():
            try:
                pkgs = fetch_untranslated(lang, force_refresh=force)
                self._loading = False
                GLib.idle_add(self._on_packages_loaded, pkgs)
            except Exception as exc:
                self._loading = False
                GLib.idle_add(self._on_load_error, str(exc))

        threading.Thread(target=do_fetch, daemon=True).start()

    def _on_packages_loaded(self, pkgs):
        self._progress_bar.set_fraction(1.0)
        self._progress_bar.set_text(
            _("{n} packages loaded").format(n=len(pkgs))
        )
        # Hide progress bar after a short delay
        GLib.timeout_add(1500, self._hide_progress)
        # Sort
        pkgs.sort(key=lambda p: p.get("package", "").lower(), reverse=not self._sort_ascending)
        self._populate_list(pkgs)

    def _on_load_error(self, msg):
        self._progress_bar.set_visible(False)
        self.status_label.set_text(msg)

    def _hide_progress(self):
        self._progress_bar.set_visible(False)
        return False

    def _clear_list(self):
        if hasattr(self.pkg_list, "remove_all"):
            self.pkg_list.remove_all()
        else:
            while True:
                row = self.pkg_list.get_row_at_index(0)
                if row is None:
                    break
                self.pkg_list.remove(row)

    def _on_heatmap_toggled(self, btn):
        self._heatmap_mode = btn.get_active()
        self._sidebar_stack.set_visible_child_name("heatmap" if self._heatmap_mode else "list")

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

        # Rebuild heatmap
        while True:
            child = self._heatmap_flow.get_first_child()
            if child is None:
                break
            self._heatmap_flow.remove(child)
        for i, pkg in enumerate(pkgs):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            box.set_size_request(100, 44)
            box.add_css_class("heatmap-red")  # all untranslated
            box.set_margin_start(2)
            box.set_margin_end(2)
            box.set_margin_top(2)
            box.set_margin_bottom(2)
            lbl = Gtk.Label(label=pkg["package"])
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_max_width_chars(14)
            lbl.set_margin_top(4)
            lbl.set_margin_start(4)
            lbl.set_margin_end(4)
            lbl.set_margin_bottom(4)
            box.append(lbl)
            box.set_tooltip_text(pkg["package"])
            gesture = Gtk.GestureClick()
            gesture.connect("released", lambda g, n, x, y, idx=i: self._select_pkg_by_index(idx))
            box.add_controller(gesture)
            box.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            self._heatmap_flow.append(box)

    def _select_pkg_by_index(self, idx):
        row = self.pkg_list.get_row_at_index(idx)
        if row:
            self.pkg_list.select_row(row)
            self._on_pkg_selected(self.pkg_list, row)

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

        # Check SMTP is configured
        settings = load_settings()
        if not settings.get("smtp_host"):
            self.status_label.set_text(_("SMTP not configured — open Preferences first"))
            return

        # Rate limiting: check delay
        send_delay = settings.get("send_delay", DEFAULT_SEND_DELAY)
        now = time.time()
        elapsed = now - self._last_send_time
        remaining = send_delay - elapsed

        if self._last_send_time > 0 and remaining > 0:
            self.status_label.set_text(
                _("Please wait {s} seconds before sending again").format(s=int(remaining + 1))
            )
            # Start a countdown timer
            self.submit_btn.set_sensitive(False)
            self._start_countdown(remaining, text)
            return

        self._do_send(text)

    def _start_countdown(self, remaining, text):
        """Count down and auto-send when ready."""
        self._countdown_remaining = remaining
        self._pending_text = text

        def tick():
            self._countdown_remaining -= 1
            if self._countdown_remaining <= 0:
                self.submit_btn.set_sensitive(True)
                self.status_label.set_text(_("Ready — sending…"))
                self._do_send(self._pending_text)
                return False
            self.status_label.set_text(
                _("Rate limit: sending in {s} seconds…").format(s=int(self._countdown_remaining))
            )
            return True

        GLib.timeout_add(1000, tick)

    def _do_send(self, text):
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
                self._last_send_time = time.time()
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
        self._first_run_shown = False

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = MainWindow(self)
            # Show welcome dialog on first run
            settings = load_settings()
            if not settings.get("welcome_shown"):
                GLib.idle_add(self._show_welcome, win)
        win.present()

    def _show_welcome(self, win):
        dialog = Adw.MessageDialog(
            transient_for=win,
            heading=_("Welcome to DDTP Translate"),
            body=_(
                "This app helps you translate Debian package descriptions "
                "through the Debian Description Translation Project (DDTP).\n\n"
                "How it works:\n"
                "1. Select your language from the dropdown\n"
                "2. Browse packages that need translation\n"
                "3. Write your translation in the editor\n"
                "4. Submit — the translation is emailed to DDTP for review\n\n"
                "Your translations help millions of Debian and Ubuntu users "
                "see package descriptions in their own language.\n\n"
                "Before submitting, you need to configure an SMTP server "
                "in Preferences (Gmail works well with an App Password)."
            ),
        )
        dialog.add_response("close", _("Get Started"))
        dialog.set_response_appearance("close", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("close")

        def on_response(d, response):
            d.close()
            settings = load_settings()
            settings["welcome_shown"] = True
            save_settings(settings)

        dialog.connect("response", on_response)
        dialog.present()

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
            translate_url="https://app.transifex.com/danielnylander/ddtp-translate/",
            translator_credits="Daniel Nylander <daniel@danielnylander.se>",
            comments=_("Translate Debian package descriptions via DDTP"),
        )
        about.present()


def main():
    app = DDTPTranslateApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
