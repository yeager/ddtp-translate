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


def _setup_css():
    css = b"""
    .heatmap-green { background-color: #26a269; color: white; border-radius: 8px; }
    .heatmap-red { background-color: #c01c28; color: white; border-radius: 8px; }
    .heatmap-gray { background-color: #77767b; color: white; border-radius: 8px; }
    .queue-ready { background-color: alpha(@accent_bg_color, 0.15); }
    .queue-sent { background-color: alpha(@success_bg_color, 0.15); }
    .queue-error { background-color: alpha(@error_bg_color, 0.15); }
    .queue-count { font-weight: bold; font-size: 1.1em; }
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

        self.add(smtp_page)

        # Sending page
        send_page = Adw.PreferencesPage(title=_("Sending"), icon_name="preferences-system-time-symbolic")
        send_group = Adw.PreferencesGroup(
            title=_("Rate Limiting"),
            description=_("Delay between email submissions to avoid flooding the DDTP server."),
        )

        self.delay_row = Adw.SpinRow.new_with_range(5, 300, 5)
        self.delay_row.set_title(_("Delay between emails (seconds)"))
        self.delay_row.set_value(self.settings.get("send_delay", DEFAULT_SEND_DELAY))
        send_group.add(self.delay_row)

        send_page.add(send_group)

        # Max packages setting
        display_group = Adw.PreferencesGroup(
            title=_("Display"),
            description=_("Limit how many packages are loaded into the list for faster startup."),
        )

        self.max_pkg_row = Adw.ComboRow(title=_("Max packages to display"))
        max_pkg_model = Gtk.StringList()
        self._max_pkg_values = [500, 1000, 5000, 0]  # 0 = all
        for v in self._max_pkg_values:
            max_pkg_model.append(str(v) if v > 0 else _("All"))
        self.max_pkg_row.set_model(max_pkg_model)
        current_max = self.settings.get("max_packages", 500)
        if current_max in self._max_pkg_values:
            self.max_pkg_row.set_selected(self._max_pkg_values.index(current_max))
        else:
            self.max_pkg_row.set_selected(0)
        display_group.add(self.max_pkg_row)

        send_page.add(display_group)
        self.add(send_page)

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
                "max_packages": self._max_pkg_values[self.max_pkg_row.get_selected()],
            }
        )
        save_settings(self.settings)
        return False


# --- Queue item ---
class QueueItem:
    """A translation queued for submission."""
    STATUS_READY = "ready"
    STATUS_SENDING = "sending"
    STATUS_SENT = "sent"
    STATUS_ERROR = "error"

    def __init__(self, package, md5, short, long_text=""):
        self.package = package
        self.md5 = md5
        self.short = short
        self.long_text = long_text
        self.status = self.STATUS_READY
        self.error_msg = ""


class MainWindow(Adw.ApplicationWindow):
    """Main application window."""

    def __init__(self, app, **kwargs):
        super().__init__(application=app, title=_("DDTP Translate"), default_width=1200, default_height=750, **kwargs)

        self.packages = []
        self.current_pkg = None
        self.settings = load_settings()
        self._heatmap_mode = False
        self._sort_ascending = True
        self._last_send_time = 0
        self._queue = []  # list of QueueItem
        self._batch_running = False
        self._batch_cancel = False

        _setup_css()

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

        # Queue badge
        self._queue_label = Gtk.Label(label="")
        self._queue_label.add_css_class("queue-count")
        self._queue_label.set_visible(False)
        header.pack_end(self._queue_label)

        # Send queue button
        self._send_queue_btn = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text=_("Send queue"))
        self._send_queue_btn.add_css_class("suggested-action")
        self._send_queue_btn.set_sensitive(False)
        self._send_queue_btn.connect("clicked", self._on_send_queue)
        header.pack_end(self._send_queue_btn)

        # Import PO button
        import_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text=_("Import translated PO file"))
        import_btn.connect("clicked", self._on_import_po)
        header.pack_end(import_btn)

        # Export PO button
        export_btn = Gtk.Button(icon_name="document-save-symbolic", tooltip_text=_("Export as PO file"))
        export_btn.connect("clicked", self._on_export_po)
        header.pack_end(export_btn)

        # Hamburger menu
        menu = Gio.Menu()
        menu.append(_("Export as PO file…"), "app.export-po")
        menu.append(_("Import translated PO…"), "app.import-po")
        menu.append(_("Clear queue"), "app.clear-queue")
        menu.append(_("Preferences"), "app.preferences")
        menu.append(_("About"), "app.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_btn)

        # Main content: 3-pane — sidebar | editor | queue
        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        content_box.set_vexpand(True)
        main_box.append(content_box)

        # === LEFT: Sidebar (package list) ===
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.set_size_request(250, -1)

        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Filter packages…"))
        self.search_entry.set_margin_start(6)
        self.search_entry.set_margin_end(6)
        self.search_entry.set_margin_top(6)
        self.search_entry.set_margin_bottom(6)
        self.search_entry.connect("search-changed", self._on_search_changed)
        sidebar_box.append(self.search_entry)

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_margin_start(6)
        self._progress_bar.set_margin_end(6)
        self._progress_bar.set_visible(False)
        sidebar_box.append(self._progress_bar)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.pkg_list = Gtk.ListBox()
        self.pkg_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.pkg_list.connect("row-selected", self._on_pkg_selected)
        scroll.set_child(self.pkg_list)

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

        content_box.append(sidebar_box)
        content_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # === CENTER: Editor ===
        editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        editor_box.set_hexpand(True)

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

        # Add to queue button
        self._add_queue_btn = Gtk.Button(label=_("Add to Queue"))
        self._add_queue_btn.set_tooltip_text(_("Add this translation to the send queue"))
        self._add_queue_btn.set_sensitive(False)
        self._add_queue_btn.connect("clicked", self._on_add_to_queue)
        bottom.append(self._add_queue_btn)

        # Direct submit button
        self.submit_btn = Gtk.Button(label=_("Submit Now"))
        self.submit_btn.add_css_class("suggested-action")
        self.submit_btn.set_sensitive(False)
        self.submit_btn.connect("clicked", self._on_submit)
        bottom.append(self.submit_btn)

        editor_box.append(bottom)
        content_box.append(editor_box)
        content_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # === RIGHT: Queue panel ===
        queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        queue_box.set_size_request(280, -1)

        queue_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        queue_header.set_margin_start(8)
        queue_header.set_margin_end(8)
        queue_header.set_margin_top(8)
        queue_header.set_margin_bottom(4)

        queue_title = Gtk.Label(label=_("Send Queue"), xalign=0, hexpand=True)
        queue_title.add_css_class("heading")
        queue_header.append(queue_title)

        self._queue_count_label = Gtk.Label(label="0")
        self._queue_count_label.add_css_class("dim-label")
        queue_header.append(self._queue_count_label)

        # Sort queue button
        queue_sort_btn = Gtk.Button(icon_name="view-sort-ascending-symbolic", tooltip_text=_("Sort queue"))
        queue_sort_btn.connect("clicked", self._on_sort_queue)
        queue_header.append(queue_sort_btn)

        queue_box.append(queue_header)

        # Queue list
        queue_scroll = Gtk.ScrolledWindow(vexpand=True)
        self._queue_list = Gtk.ListBox()
        self._queue_list.set_selection_mode(Gtk.SelectionMode.NONE)
        queue_scroll.set_child(self._queue_list)
        queue_box.append(queue_scroll)

        # Queue status bar
        self._queue_status = Gtk.Label(label="", xalign=0)
        self._queue_status.add_css_class("dim-label")
        self._queue_status.set_margin_start(8)
        self._queue_status.set_margin_end(8)
        self._queue_status.set_margin_top(4)
        self._queue_status.set_margin_bottom(8)
        queue_box.append(self._queue_status)

        content_box.append(queue_box)

        # Load initial data
        self._refresh_packages()

    # --- Helpers ---

    def _current_lang(self):
        idx = self.lang_dropdown.get_selected()
        if 0 <= idx < len(self._lang_codes):
            return self._lang_codes[idx]
        return "sv"

    def _format_duration(self, seconds):
        if seconds < 60:
            return _("{s} seconds").format(s=int(seconds))
        minutes = seconds / 60
        if minutes < 60:
            return _("{m} minutes").format(m=int(minutes))
        hours = minutes / 60
        return _("{h}h {m}m").format(h=int(hours), m=int(minutes % 60))

    # --- Package list ---

    def _on_lang_changed(self, *_args):
        self._refresh_packages()

    def _on_refresh(self, *_args):
        self._refresh_packages(force=True)

    def _on_sort_clicked(self, btn):
        self._sort_ascending = not self._sort_ascending
        btn.set_icon_name(
            "view-sort-ascending-symbolic" if self._sort_ascending else "view-sort-descending-symbolic"
        )
        if hasattr(self, '_all_packages'):
            self._all_packages.sort(key=lambda p: p.get("package", "").lower(), reverse=not self._sort_ascending)
            self._on_packages_loaded(self._all_packages)
        else:
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

    def _get_max_packages(self):
        """Get max packages limit from settings (0 = all)."""
        return self.settings.get("max_packages", 500)

    def _on_packages_loaded(self, pkgs):
        total = len(pkgs)
        self._progress_bar.set_fraction(1.0)
        self._progress_bar.set_text(_("{n} packages loaded").format(n=total))
        GLib.timeout_add(1500, self._hide_progress)
        pkgs.sort(key=lambda p: p.get("package", "").lower(), reverse=not self._sort_ascending)
        self._all_packages = pkgs  # keep full list for export/search
        limit = self._get_max_packages()
        if limit > 0 and len(pkgs) > limit:
            display_pkgs = pkgs[:limit]
            self.stats_label.set_text(_("{shown} of {total} untranslated").format(shown=limit, total=total))
            self._populate_list(display_pkgs, update_stats=False)
        else:
            display_pkgs = pkgs
            self._populate_list(display_pkgs)

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

    def _populate_list(self, pkgs, update_stats=True):
        self.packages = pkgs
        self._clear_list()
        for pkg in pkgs:
            label = Gtk.Label(label=pkg["package"], xalign=0)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            self.pkg_list.append(label)
        if update_stats:
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
            box.add_css_class("heatmap-red")
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
            self._add_queue_btn.set_sensitive(False)
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
            self._add_queue_btn.set_sensitive(True)
            self.status_label.set_text(_("Editing: {pkg}").format(pkg=pkg["package"]))

    # --- Single submit ---

    def _on_submit(self, *_args):
        if not self.current_pkg:
            return

        buf = self.trans_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text:
            self.status_label.set_text(_("Translation is empty"))
            return

        settings = load_settings()
        if not settings.get("smtp_host"):
            self.status_label.set_text(_("SMTP not configured — open Preferences first"))
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
                self._last_send_time = time.time()
                GLib.idle_add(self.status_label.set_text, _("Sent successfully!"))
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text, _("Error: {e}").format(e=str(exc)))
            GLib.idle_add(self.submit_btn.set_sensitive, True)

        threading.Thread(target=do_send, daemon=True).start()

    # --- Queue management ---

    def _on_add_to_queue(self, *_args):
        """Add current translation to the send queue."""
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

        # Check if already in queue
        for item in self._queue:
            if item.package == pkg["package"] and item.md5 == pkg["md5"]:
                item.short = short
                item.long_text = long_text
                item.status = QueueItem.STATUS_READY
                item.error_msg = ""
                self._rebuild_queue_ui()
                self.status_label.set_text(_("Updated {pkg} in queue").format(pkg=pkg["package"]))
                return

        self._queue.append(QueueItem(pkg["package"], pkg["md5"], short, long_text))
        self._rebuild_queue_ui()
        self.status_label.set_text(_("Added {pkg} to queue ({n} total)").format(
            pkg=pkg["package"], n=len(self._queue)))

    def _on_sort_queue(self, *_args):
        self._queue.sort(key=lambda q: q.package.lower())
        self._rebuild_queue_ui()

    def _clear_queue(self):
        self._queue = [q for q in self._queue if q.status == QueueItem.STATUS_SENDING]
        self._rebuild_queue_ui()

    def _rebuild_queue_ui(self):
        """Rebuild the queue list UI."""
        # Clear
        if hasattr(self._queue_list, "remove_all"):
            self._queue_list.remove_all()
        else:
            while True:
                row = self._queue_list.get_row_at_index(0)
                if row is None:
                    break
                self._queue_list.remove(row)

        ready_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_READY)
        sent_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_SENT)
        error_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_ERROR)

        for i, item in enumerate(self._queue):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row_box.set_margin_start(8)
            row_box.set_margin_end(8)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)

            # Status icon
            if item.status == QueueItem.STATUS_READY:
                icon = Gtk.Image.new_from_icon_name("mail-unread-symbolic")
                row_box.add_css_class("queue-ready")
            elif item.status == QueueItem.STATUS_SENDING:
                icon = Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic")
            elif item.status == QueueItem.STATUS_SENT:
                icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
                row_box.add_css_class("queue-sent")
            else:
                icon = Gtk.Image.new_from_icon_name("dialog-error-symbolic")
                row_box.add_css_class("queue-error")
            row_box.append(icon)

            # Package name
            name_label = Gtk.Label(label=item.package, xalign=0, hexpand=True)
            name_label.set_ellipsize(Pango.EllipsizeMode.END)
            if item.error_msg:
                name_label.set_tooltip_text(item.error_msg)
            row_box.append(name_label)

            # Remove button (only if not currently sending)
            if item.status != QueueItem.STATUS_SENDING:
                remove_btn = Gtk.Button(icon_name="user-trash-symbolic")
                remove_btn.add_css_class("flat")
                remove_btn.set_tooltip_text(_("Remove from queue"))
                remove_btn.connect("clicked", lambda b, idx=i: self._remove_queue_item(idx))
                row_box.append(remove_btn)

            self._queue_list.append(row_box)

        self._queue_count_label.set_text(
            _("{ready} ready, {sent} sent, {errors} errors").format(
                ready=ready_count, sent=sent_count, errors=error_count)
        )

        # Update badge
        if ready_count > 0:
            self._queue_label.set_text(f"📬 {ready_count}")
            self._queue_label.set_visible(True)
            self._send_queue_btn.set_sensitive(not self._batch_running)
        else:
            self._queue_label.set_visible(False)
            self._send_queue_btn.set_sensitive(False)

    def _remove_queue_item(self, idx):
        if 0 <= idx < len(self._queue):
            removed = self._queue.pop(idx)
            self._rebuild_queue_ui()
            self.status_label.set_text(_("Removed {pkg} from queue").format(pkg=removed.package))

    # --- Batch send with confirmation ---

    def _on_send_queue(self, *_args):
        ready = [q for q in self._queue if q.status == QueueItem.STATUS_READY]
        if not ready:
            return

        settings = load_settings()
        if not settings.get("smtp_host"):
            self.status_label.set_text(_("SMTP not configured — open Preferences first"))
            return

        delay = settings.get("send_delay", DEFAULT_SEND_DELAY)
        total = len(ready)
        est_time = self._format_duration(total * delay)

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Send {n} translations to DDTP?").format(n=total),
            body=_(
                "You are about to send {n} translation(s) to the Debian Description "
                "Translation Project (DDTP) via email.\n\n"
                "⚠️ Important information:\n\n"
                "• Each translation is sent as a separate email to pdesc@ddtp.debian.org\n"
                "• A delay of {delay} seconds is enforced between each email "
                "to prevent overloading the DDTP server\n"
                "• The DDTP server is run by volunteers — please be considerate\n"
                "• Estimated time: {time}\n"
                "• You can cancel the process at any time\n"
                "• Successfully sent translations cannot be recalled\n"
                "• Any SMTP errors will be shown per package\n\n"
                "The delay can be adjusted in Preferences → Sending.\n"
                "Make sure your SMTP settings and email address are correct."
            ).format(n=total, delay=delay, time=est_time),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("send", _("I understand — Send All"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(d, response):
            d.close()
            if response == "send":
                self._start_batch_send()

        dialog.connect("response", on_response)
        dialog.present()

    def _start_batch_send(self):
        """Start sending all ready items in the queue."""
        self._batch_running = True
        self._batch_cancel = False
        self._send_queue_btn.set_sensitive(False)

        # Show a progress dialog
        self._batch_dialog = Adw.Window(
            transient_for=self,
            title=_("Sending Translations"),
            default_width=500,
            default_height=400,
            modal=True,
        )

        dialog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        dialog_box.set_margin_start(24)
        dialog_box.set_margin_end(24)
        dialog_box.set_margin_top(24)
        dialog_box.set_margin_bottom(24)

        # Header
        self._batch_heading = Gtk.Label(label=_("Sending translations…"))
        self._batch_heading.add_css_class("title-2")
        dialog_box.append(self._batch_heading)

        # Overall progress
        self._batch_progress = Gtk.ProgressBar()
        self._batch_progress.set_show_text(True)
        dialog_box.append(self._batch_progress)

        # Current package
        self._batch_current = Gtk.Label(label="", xalign=0)
        self._batch_current.add_css_class("dim-label")
        dialog_box.append(self._batch_current)

        # Countdown
        self._batch_countdown = Gtk.Label(label="", xalign=0)
        dialog_box.append(self._batch_countdown)

        # Log
        log_scroll = Gtk.ScrolledWindow(vexpand=True)
        self._batch_log_buf = Gtk.TextBuffer()
        log_view = Gtk.TextView(buffer=self._batch_log_buf, editable=False, wrap_mode=Gtk.WrapMode.WORD)
        log_view.add_css_class("monospace")
        log_scroll.set_child(log_view)
        dialog_box.append(log_scroll)

        # Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)

        self._batch_cancel_btn = Gtk.Button(label=_("Cancel"))
        self._batch_cancel_btn.add_css_class("destructive-action")
        self._batch_cancel_btn.connect("clicked", self._on_batch_cancel)
        btn_box.append(self._batch_cancel_btn)

        self._batch_close_btn = Gtk.Button(label=_("Close"))
        self._batch_close_btn.set_sensitive(False)
        self._batch_close_btn.connect("clicked", lambda b: self._batch_dialog.close())
        btn_box.append(self._batch_close_btn)

        dialog_box.append(btn_box)

        self._batch_dialog.set_content(dialog_box)
        self._batch_dialog.present()

        # Start sending in background
        threading.Thread(target=self._batch_send_worker, daemon=True).start()

    def _batch_log(self, text):
        """Append text to the batch log."""
        def do_log():
            end = self._batch_log_buf.get_end_iter()
            self._batch_log_buf.insert(end, text + "\n")
        GLib.idle_add(do_log)

    def _on_batch_cancel(self, *_args):
        self._batch_cancel = True
        self._batch_cancel_btn.set_sensitive(False)
        self._batch_log(_("⏸ Cancelling after current email…"))

    def _batch_send_worker(self):
        """Background worker that sends queued translations."""
        settings = load_settings()
        delay = settings.get("send_delay", DEFAULT_SEND_DELAY)
        lang = self._current_lang()

        ready = [q for q in self._queue if q.status == QueueItem.STATUS_READY]
        total = len(ready)
        sent = 0
        errors = 0

        for i, item in enumerate(ready):
            if self._batch_cancel:
                self._batch_log(_("❌ Cancelled by user. {sent}/{total} sent.").format(
                    sent=sent, total=total))
                break

            item.status = QueueItem.STATUS_SENDING
            GLib.idle_add(self._rebuild_queue_ui)
            GLib.idle_add(self._batch_current.set_text,
                          _("Sending: {pkg} ({i}/{total})").format(pkg=item.package, i=i + 1, total=total))
            GLib.idle_add(self._batch_progress.set_fraction, (i + 0.5) / total)
            GLib.idle_add(self._batch_progress.set_text, f"{i + 1}/{total}")

            try:
                send_translation(item.package, item.md5, lang, item.short, item.long_text, settings)
                item.status = QueueItem.STATUS_SENT
                sent += 1
                self._batch_log(f"✅ {item.package}")
            except Exception as exc:
                item.status = QueueItem.STATUS_ERROR
                item.error_msg = str(exc)
                errors += 1
                self._batch_log(f"❌ {item.package}: {exc}")

            GLib.idle_add(self._rebuild_queue_ui)
            GLib.idle_add(self._batch_progress.set_fraction, (i + 1) / total)

            # Rate limit delay (not after last or if cancelled)
            if i < total - 1 and not self._batch_cancel and delay > 0:
                for sec in range(int(delay), 0, -1):
                    if self._batch_cancel:
                        break
                    GLib.idle_add(self._batch_countdown.set_text,
                                  _("Next email in {s} seconds…").format(s=sec))
                    time.sleep(1)
                GLib.idle_add(self._batch_countdown.set_text, "")

        # Done
        self._batch_running = False
        summary = _("Done! {sent} sent, {errors} errors out of {total}").format(
            sent=sent, errors=errors, total=total)
        self._batch_log(f"\n{summary}")
        GLib.idle_add(self._batch_heading.set_text, _("Sending complete"))
        GLib.idle_add(self._batch_current.set_text, summary)
        GLib.idle_add(self._batch_countdown.set_text, "")
        GLib.idle_add(self._batch_cancel_btn.set_sensitive, False)
        GLib.idle_add(self._batch_close_btn.set_sensitive, True)
        GLib.idle_add(self._rebuild_queue_ui)
        GLib.idle_add(self.status_label.set_text, summary)

    # --- PO Export/Import ---

    def _on_export_po(self, *_args):
        if not self.packages:
            self.status_label.set_text(_("No packages loaded to export"))
            return

        lang = self._current_lang()
        dialog = Gtk.FileDialog()
        dialog.set_initial_name(f"ddtp-{lang}.po")
        fil = Gtk.FileFilter()
        fil.set_name(_("PO files"))
        fil.add_pattern("*.po")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(fil)
        dialog.set_filters(filters)
        dialog.save(self, None, self._on_export_po_response)

    def _on_export_po_response(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return

        path = gfile.get_path()
        lang = self._current_lang()
        export_pkgs = getattr(self, '_all_packages', self.packages)
        lines = [
            '# DDTP translations export',
            f'# Language: {lang}',
            f'# Packages: {len(export_pkgs)}',
            '#',
            'msgid ""', 'msgstr ""',
            f'"Language: {lang}\\n"',
            '"Content-Type: text/plain; charset=UTF-8\\n"',
            '"Content-Transfer-Encoding: 8bit\\n"',
            '',
        ]

        for pkg in export_pkgs:
            lines.append(f'#. Package: {pkg["package"]}')
            lines.append(f'#. MD5: {pkg["md5"]}')
            lines.append(f'msgid "{self._po_escape(pkg["short"])}"')
            lines.append('msgstr ""')
            lines.append('')
            if pkg["long"]:
                lines.append(f'#. Long description for {pkg["package"]}')
                lines.append(f'msgctxt "long:{pkg["package"]}"')
                escaped = self._po_escape_multiline(pkg["long"])
                lines.append(f'msgid {escaped}')
                lines.append('msgstr ""')
                lines.append('')

        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

        self.status_label.set_text(
            _("Exported {n} packages to {path}").format(n=len(export_pkgs), path=os.path.basename(path)))

    def _po_escape(self, s):
        return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

    def _po_escape_multiline(self, s):
        lines = s.split('\n')
        if len(lines) == 1:
            return f'"{self._po_escape(s)}"'
        parts = ['""']
        for i, line in enumerate(lines):
            escaped = self._po_escape(line)
            if i < len(lines) - 1:
                parts.append(f'"{escaped}\\n"')
            else:
                parts.append(f'"{escaped}"')
        return '\n'.join(parts)

    def _on_import_po(self, *_args):
        dialog = Gtk.FileDialog()
        fil = Gtk.FileFilter()
        fil.set_name(_("PO files"))
        fil.add_pattern("*.po")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(fil)
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_import_po_response)

    def _on_import_po_response(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return

        path = gfile.get_path()
        translations = self._parse_imported_po(path)

        if not translations:
            self.status_label.set_text(_("No translated entries found in file (only entries with translations are imported)"))
            return

        # Add all to queue
        added = 0
        updated = 0
        for pkg, md5, short, long_text in translations:
            existing = None
            for q in self._queue:
                if q.package == pkg and q.md5 == md5:
                    existing = q
                    break
            if existing:
                existing.short = short
                existing.long_text = long_text
                existing.status = QueueItem.STATUS_READY
                existing.error_msg = ""
                updated += 1
            else:
                self._queue.append(QueueItem(pkg, md5, short, long_text))
                added += 1

        self._rebuild_queue_ui()
        self.status_label.set_text(
            _("Imported {added} new, {updated} updated — {total} in queue").format(
                added=added, updated=updated, total=len(self._queue)))

    def _parse_imported_po(self, path):
        """Parse PO file, return only entries that have actual translations."""
        translations = []
        current_pkg = None
        current_md5 = None
        current_context = None
        in_msgstr = False
        msgstr_lines = []

        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        def flush():
            nonlocal current_pkg, current_md5, current_context, msgstr_lines
            text = self._po_unescape_joined(msgstr_lines).strip()
            if not text or not current_pkg or not current_md5:
                return

            entry = None
            for t in translations:
                if t[0] == current_pkg and t[1] == current_md5:
                    entry = t
                    break
            if current_context and current_context.startswith("long:"):
                if entry:
                    translations[translations.index(entry)] = (entry[0], entry[1], entry[2], text)
                else:
                    translations.append((current_pkg, current_md5, "", text))
            else:
                if entry:
                    translations[translations.index(entry)] = (entry[0], entry[1], text, entry[3])
                else:
                    translations.append((current_pkg, current_md5, text, ""))

        for line in lines:
            line = line.rstrip('\n')
            if line.startswith('#. Package: '):
                current_pkg = line[12:].strip()
            elif line.startswith('#. MD5: '):
                current_md5 = line[8:].strip()
            elif line.startswith('msgctxt "long:'):
                current_context = line.split('"')[1]
            elif line.startswith('msgctxt '):
                current_context = line.split('"')[1] if '"' in line else None
            elif line.startswith('msgid '):
                if in_msgstr:
                    flush()
                    msgstr_lines = []
                in_msgstr = False
            elif line.startswith('msgstr '):
                in_msgstr = True
                msgstr_lines = [line[7:].strip().strip('"')]
            elif in_msgstr and line.startswith('"'):
                msgstr_lines.append(line.strip().strip('"'))
            elif not line.strip():
                if in_msgstr:
                    flush()
                    msgstr_lines = []
                    in_msgstr = False
                    current_context = None

        if in_msgstr:
            flush()

        # ONLY return entries with a non-empty short description
        return [(p, m, s, l) for p, m, s, l in translations if s]

    def _po_unescape_joined(self, parts):
        raw = ''.join(parts)
        return raw.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')


class DDTPTranslateApp(Adw.Application):
    """Main application."""

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

        self.create_action("preferences", self._on_preferences)
        self.create_action("about", self._on_about)
        self.create_action("export-po", self._on_export_po_action)
        self.create_action("import-po", self._on_import_po_action)
        self.create_action("clear-queue", self._on_clear_queue_action)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = MainWindow(self)
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
                "4. Add to queue or submit directly\n"
                "5. Send the queue — translations are emailed to DDTP\n\n"
                "Tip: Export as PO, translate in your favorite editor, "
                "then import back to queue many at once.\n\n"
                "Before submitting, configure SMTP in Preferences "
                "(Gmail works well with an App Password)."
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

    def _on_export_po_action(self, *_args):
        win = self.props.active_window
        if win:
            win._on_export_po()

    def _on_import_po_action(self, *_args):
        win = self.props.active_window
        if win:
            win._on_import_po()

    def _on_clear_queue_action(self, *_args):
        win = self.props.active_window
        if win:
            win._clear_queue()

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
            copyright="© 2026 Daniel Nylander",
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
