#!/usr/bin/env python3
"""DDTP Translate — GTK4/Adwaita app for translating Debian package descriptions."""

import gettext
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Gdk, Pango  # noqa: E402

from . import __version__
from .ddtp_api import DDTP_LANGUAGES, fetch_untranslated, fetch_ddtp_stats, fetch_popcon_data
from .settings import load_settings, save_settings
from .ddtss_client import (
    DDTSSClient, DDTSSError, DDTSSAuthError,
    DDTSSLockedError, DDTSSValidationError,
)

# i18n setup
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass

LOCALE_DIR = None
for d in [
    os.path.join(os.path.dirname(__file__), "..", "..", "po"),
    "/usr/share/locale",
    "/usr/local/share/locale",
]:
    if os.path.isdir(d):
        LOCALE_DIR = d
        break

locale.bindtextdomain("ddtp-translate", LOCALE_DIR)
gettext.bindtextdomain("ddtp-translate", LOCALE_DIR)
gettext.textdomain("ddtp-translate")
_ = gettext.gettext

APP_ID = "se.danielnylander.ddtp-translate"


# --- Data directory helpers ---

def _po_escape(s):
    """Escape a string for use in a PO file msgid/msgstr."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _parse_po_entries(path):
    """Parse a .po file and return list of (msgid, msgstr) tuples, skipping the header."""
    entries = []
    current_id = []
    current_str = []
    in_msgid = False
    in_msgstr = False

    def _unescape(s):
        return s.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")

    def _flush():
        mid = _unescape("".join(current_id))
        mstr = _unescape("".join(current_str))
        if mid:  # skip header (empty msgid)
            entries.append((mid, mstr))

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("msgid "):
                if in_msgstr:
                    _flush()
                    current_id.clear()
                    current_str.clear()
                in_msgid = True
                in_msgstr = False
                s = line[6:].strip()
                if s.startswith('"') and s.endswith('"'):
                    current_id.append(s[1:-1])
            elif line.startswith("msgstr "):
                in_msgid = False
                in_msgstr = True
                s = line[7:].strip()
                if s.startswith('"') and s.endswith('"'):
                    current_str.append(s[1:-1])
            elif line.startswith('"') and line.endswith('"'):
                s = line[1:-1]
                if in_msgid:
                    current_id.append(s)
                elif in_msgstr:
                    current_str.append(s)
        if in_msgstr:
            _flush()

    return entries


def _data_dir():
    xdg = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    d = os.path.join(xdg, "ddtp-translate")
    os.makedirs(d, exist_ok=True)
    return d


def _queue_path():
    return os.path.join(_data_dir(), "queue.json")


def _log_path():
    return os.path.join(_data_dir(), "events.log")


def _log_event(message):
    """Log an event if logging is enabled."""
    settings = load_settings()
    if not settings.get("enable_logging", False):
        return
    import datetime
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


def _save_queue(queue):
    """Persist queue to disk."""
    import json
    data = []
    for item in queue:
        data.append({
            "package": item.package,
            "md5": item.md5,
            "short": item.short,
            "long_text": item.long_text,
            "status": item.status,
            "error_msg": item.error_msg,
        })
    try:
        with open(_queue_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _load_queue():
    """Load queue from disk."""
    import json
    path = _queue_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = []
        for d in data:
            item = QueueItem(d["package"], d["md5"], d["short"], d.get("long_text", ""))
            item.status = d.get("status", QueueItem.STATUS_READY)
            item.error_msg = d.get("error_msg", "")
            items.append(item)
        return items
    except (OSError, json.JSONDecodeError, KeyError):
        return []


def _setup_css():
    css = b"""
    .heatmap-green { background-color: #26a269; color: white; border-radius: 8px; }
    .heatmap-red { background-color: #c01c28; color: white; border-radius: 8px; }
    .heatmap-gray { background-color: #77767b; color: white; border-radius: 8px; }
    .queue-ready { background-color: alpha(@accent_bg_color, 0.15); }
    .queue-sent { background-color: alpha(@success_bg_color, 0.15); }
    .queue-error { background-color: alpha(@error_bg_color, 0.15); }
    .queue-count { font-weight: bold; font-size: 1.1em; }
    .pkg-flag-submitted { color: #26a269; }
    .pkg-flag-modified { color: #e5a50a; }
    .pkg-flag-queued { color: @accent_color; }
    .pkg-flag-error { color: #c01c28; }
    .pkg-status-none { color: #3584e4; }
    .pkg-status-pending { color: #e5a50a; }
    .pkg-status-reviewed-comment { color: #ff7800; }
    .pkg-status-reviewed-ok { color: #26a269; }
    .status-bar { padding: 4px 12px; }
    .pkg-banner { padding: 4px 12px; }
    .compact-row { padding: 2px 6px; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


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


# --- Preferences Window ---

class PreferencesWindow(Adw.PreferencesWindow):
    """Application settings."""

    def __init__(self, parent, **kwargs):
        super().__init__(
            title=_("Preferences"),
            transient_for=parent,
            **kwargs,
        )
        self.settings = load_settings()

        # DDTSS page
        ddtss_page = Adw.PreferencesPage(title=_("DDTSS"), icon_name="web-browser-symbolic")

        ddtss_group = Adw.PreferencesGroup(
            title=_("DDTSS Account"),
            description=_("Create an account at https://ddtp.debian.org/ddtss/index.cgi/createlogin"),
        )
        self.ddtss_alias_row = Adw.EntryRow(title=_("Alias"))
        self.ddtss_alias_row.set_text(self.settings.get("ddtss_alias", ""))
        ddtss_group.add(self.ddtss_alias_row)

        self.ddtss_pass_row = Adw.PasswordEntryRow(title=_("Password"))
        self.ddtss_pass_row.set_text(self.settings.get("ddtss_password", ""))
        ddtss_group.add(self.ddtss_pass_row)

        ddtss_test_btn = Gtk.Button(label=_("Test Login"))
        ddtss_test_btn.add_css_class("suggested-action")
        ddtss_test_btn.add_css_class("pill")
        ddtss_test_btn.set_margin_top(8)
        ddtss_test_btn.set_margin_bottom(4)
        ddtss_test_btn.connect("clicked", self._test_ddtss_login)
        ddtss_group.add(ddtss_test_btn)

        ddtss_page.add(ddtss_group)
        self.add(ddtss_page)

        # Settings page
        settings_page = Adw.PreferencesPage(title=_("Settings"), icon_name="preferences-system-symbolic")

        display_group = Adw.PreferencesGroup(
            title=_("Display"),
            description=_("Limit how many packages are loaded into the list for faster startup."),
        )

        self.max_pkg_row = Adw.ComboRow(title=_("Max packages to display"))
        max_pkg_model = Gtk.StringList()
        self._max_pkg_values = [500, 1000, 5000, 0]
        for v in self._max_pkg_values:
            max_pkg_model.append(str(v) if v > 0 else _("All"))
        self.max_pkg_row.set_model(max_pkg_model)
        current_max = self.settings.get("max_packages", 500)
        if current_max in self._max_pkg_values:
            self.max_pkg_row.set_selected(self._max_pkg_values.index(current_max))
        else:
            self.max_pkg_row.set_selected(0)
        display_group.add(self.max_pkg_row)

        settings_page.add(display_group)

        log_group = Adw.PreferencesGroup(
            title=_("Logging"),
            description=_("Enable event logging to track application activity."),
        )
        self.logging_row = Adw.SwitchRow(title=_("Enable event logging"))
        self.logging_row.set_active(self.settings.get("enable_logging", False))
        log_group.add(self.logging_row)
        settings_page.add(log_group)

        self.add(settings_page)

        # Workflow page (v0.8.0)
        workflow_page = Adw.PreferencesPage(title=_("Workflow"), icon_name="emblem-system-symbolic")
        workflow_group = Adw.PreferencesGroup(
            title=_("Workflow"),
            description=_("Customize the translation workflow."),
        )

        self.auto_lint_row = Adw.SwitchRow(title=_("Auto-lint before submit"))
        self.auto_lint_row.set_active(self.settings.get("auto_lint", True))
        workflow_group.add(self.auto_lint_row)

        self.auto_advance_row = Adw.SwitchRow(title=_("Auto-advance to next package after submit"))
        self.auto_advance_row.set_active(self.settings.get("auto_advance", True))
        workflow_group.add(self.auto_advance_row)

        self.cache_ttl_row = Adw.SpinRow.new_with_range(1, 168, 1)
        self.cache_ttl_row.set_title(_("Cache TTL (hours)"))
        self.cache_ttl_row.set_value(self.settings.get("cache_ttl_hours", 24))
        workflow_group.add(self.cache_ttl_row)

        workflow_page.add(workflow_group)

        # Sorting group
        sort_group = Adw.PreferencesGroup(
            title=_("Sorting"),
            description=_("Default sort order for the package list."),
        )

        self.default_sort_row = Adw.ComboRow(title=_("Default sort mode"))
        sort_model = Gtk.StringList()
        self._sort_mode_values = ["alpha", "status", "popcon"]
        for label in [_("Alphabetical"), _("By status"), _("By popularity (popcon)")]:
            sort_model.append(label)
        self.default_sort_row.set_model(sort_model)
        current_sort = self.settings.get("sort_mode", "alpha")
        if current_sort in self._sort_mode_values:
            self.default_sort_row.set_selected(self._sort_mode_values.index(current_sort))
        sort_group.add(self.default_sort_row)

        self.fetch_statuses_row = Adw.SwitchRow(title=_("Fetch DDTSS statuses on load"))
        self.fetch_statuses_row.set_active(self.settings.get("fetch_ddtss_statuses", True))
        sort_group.add(self.fetch_statuses_row)

        workflow_page.add(sort_group)

        self.add(workflow_page)

        self.connect("close-request", self._on_close)

    def _test_ddtss_login(self, btn):
        alias = self.ddtss_alias_row.get_text().strip()
        password = self.ddtss_pass_row.get_text().strip()
        if not alias or not password:
            btn.set_label(_("Enter alias and password first"))
            GLib.timeout_add(2000, lambda: btn.set_label(_("Test Login")) or False)
            return

        def _do_test():
            try:
                client = DDTSSClient()
                client.login(alias, password)
                GLib.idle_add(lambda: btn.set_label("✅ " + _("Login successful!")) or False)
            except DDTSSAuthError as e:
                GLib.idle_add(lambda: btn.set_label(f"❌ {e}") or False)
            except DDTSSError as e:
                GLib.idle_add(lambda: btn.set_label(f"❌ {e}") or False)
            GLib.timeout_add(3000, lambda: btn.set_label(_("Test Login")) or False)

        threading.Thread(target=_do_test, daemon=True).start()

    def _on_close(self, *_args):
        self.settings.update(
            {
                "ddtss_alias": self.ddtss_alias_row.get_text(),
                "ddtss_password": self.ddtss_pass_row.get_text(),
                "max_packages": self._max_pkg_values[self.max_pkg_row.get_selected()],
                "enable_logging": self.logging_row.get_active(),
                "auto_lint": self.auto_lint_row.get_active(),
                "auto_advance": self.auto_advance_row.get_active(),
                "cache_ttl_hours": int(self.cache_ttl_row.get_value()),
                "sort_mode": self._sort_mode_values[self.default_sort_row.get_selected()],
                "fetch_ddtss_statuses": self.fetch_statuses_row.get_active(),
            }
        )
        save_settings(self.settings)
        return False


# --- Main Window ---

class MainWindow(Adw.ApplicationWindow):
    """Main application window with three-panel layout."""

    def __init__(self, app, **kwargs):
        super().__init__(application=app, title=_("DDTP Translate"), default_width=1200, default_height=750, **kwargs)

        self.packages = []
        self.current_pkg = None
        self.settings = load_settings()
        self._heatmap_mode = False
        self._sort_ascending = True
        self._sort_mode = self.settings.get("sort_mode", "alpha")  # alpha, status, popcon
        self._last_send_time = 0
        self._queue = []
        self._batch_running = False
        self._batch_cancel = False
        self._submitted_packages = set()
        self._modified_packages = set()
        self._error_packages = set()
        self._ddtss_logged_in = False
        self._completion_pct = 0.0

        # DDTSS status tracking
        # Status values: "none" (untranslated), "pending" (submitted, not reviewed),
        # "reviewed_comment" (reviewed with comment), "reviewed_ok" (reviewed OK/done)
        self._pkg_ddtss_status = {}  # package_name -> status string
        self._pkg_ddtss_data = {}    # package_name -> {short_trans, long_trans, ...}
        self._status_filter = "all"  # all, none, pending, reviewed_comment, reviewed_ok
        self._popcon_data = {}       # package_name -> install_count

        self.connect("close-request", self._on_close_request)

        _setup_css()

        # Main layout
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(outer_box)

        # Header bar
        header = Adw.HeaderBar()
        outer_box.append(header)

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

        # Sort mode dropdown
        sort_model = Gtk.StringList()
        self._sort_modes = ["alpha", "status", "popcon"]
        self._sort_mode_labels = [_("Alphabetical"), _("By status"), _("By popularity")]
        for label in self._sort_mode_labels:
            sort_model.append(label)
        self._sort_dropdown = Gtk.DropDown(model=sort_model)
        self._sort_dropdown.set_tooltip_text(_("Sort mode"))
        if self._sort_mode in self._sort_modes:
            self._sort_dropdown.set_selected(self._sort_modes.index(self._sort_mode))
        self._sort_dropdown.connect("notify::selected", self._on_sort_mode_changed)
        header.pack_start(self._sort_dropdown)

        # Sort direction button
        sort_btn = Gtk.Button(icon_name="view-sort-ascending-symbolic", tooltip_text=_("Sort direction"))
        sort_btn.connect("clicked", self._on_sort_clicked)
        header.pack_start(sort_btn)
        self._sort_btn = sort_btn

        # Status filter dropdown
        filter_model = Gtk.StringList()
        self._filter_values = ["all", "none", "pending", "reviewed_comment", "reviewed_ok"]
        self._filter_labels = [
            _("All packages"),
            _("🔵 Not translated"),
            _("🟡 Submitted (not reviewed)"),
            _("🟠 Reviewed (with comments)"),
            _("🟢 Reviewed OK"),
        ]
        for label in self._filter_labels:
            filter_model.append(label)
        self._filter_dropdown = Gtk.DropDown(model=filter_model)
        self._filter_dropdown.set_tooltip_text(_("Filter by status"))
        self._filter_dropdown.connect("notify::selected", self._on_filter_changed)
        header.pack_start(self._filter_dropdown)

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

        # Review badge
        self._review_badge = Gtk.Label(label="")
        self._review_badge.add_css_class("queue-count")
        self._review_badge.set_visible(False)
        header.pack_end(self._review_badge)

        # Show Queue button
        self._show_queue_btn = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text=_("Show Queue"))
        self._show_queue_btn.connect("clicked", self._on_show_queue_dialog)
        header.pack_end(self._show_queue_btn)

        # Review button
        self._review_btn = Gtk.Button(icon_name="emblem-default-symbolic", tooltip_text=_("Review translations"))
        self._review_btn.connect("clicked", self._on_open_review)
        header.pack_end(self._review_btn)

        # Stats button
        stats_btn = Gtk.Button(icon_name="utilities-system-monitor-symbolic", tooltip_text=_("Statistics"))
        stats_btn.connect("clicked", self._on_show_stats)
        header.pack_end(stats_btn)

        # Lint button
        lint_btn = Gtk.Button(icon_name="dialog-warning-symbolic", tooltip_text=_("Lint translation"))
        lint_btn.connect("clicked", self._on_lint)
        header.pack_end(lint_btn)

        # Import PO button
        import_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text=_("Import translated PO file"))
        import_btn.connect("clicked", self._on_import_po)
        header.pack_end(import_btn)

        # Export PO button
        export_btn = Gtk.Button(icon_name="document-save-symbolic", tooltip_text=_("Export as PO file"))
        export_btn.connect("clicked", self._on_export_po)
        header.pack_end(export_btn)

        # Hamburger menu with accelerator hints
        menu = Gio.Menu()

        file_section = Gio.Menu()
        file_section.append(_("Export as PO file…"), "app.export-po")
        file_section.append(_("Import translated PO…"), "app.import-po")
        menu.append_section(None, file_section)

        action_section = Gio.Menu()
        item = Gio.MenuItem.new(_("Submit Now"), "app.submit-now")
        item.set_attribute_value("accel", GLib.Variant.new_string("<Control>Return"))
        action_section.append_item(item)
        item = Gio.MenuItem.new(_("Add to Queue"), "app.add-to-queue")
        item.set_attribute_value("accel", GLib.Variant.new_string("<Control><Shift>Return"))
        action_section.append_item(item)
        item = Gio.MenuItem.new(_("Lint Translation"), "app.lint")
        item.set_attribute_value("accel", GLib.Variant.new_string("<Control>l"))
        action_section.append_item(item)
        item = Gio.MenuItem.new(_("Refresh"), "app.refresh")
        item.set_attribute_value("accel", GLib.Variant.new_string("F5"))
        action_section.append_item(item)
        menu.append_section(None, action_section)

        nav_section = Gio.Menu()
        item = Gio.MenuItem.new(_("Next Package"), "app.next-package")
        item.set_attribute_value("accel", GLib.Variant.new_string("<Control>n"))
        nav_section.append_item(item)
        item = Gio.MenuItem.new(_("Previous Package"), "app.prev-package")
        item.set_attribute_value("accel", GLib.Variant.new_string("<Control>p"))
        nav_section.append_item(item)
        menu.append_section(None, nav_section)

        misc_section = Gio.Menu()
        misc_section.append(_("Review translations"), "app.review")
        misc_section.append(_("Show Queue"), "app.show-queue")
        misc_section.append(_("Clear queue"), "app.clear-queue")
        misc_section.append(_("Statistics"), "app.stats")
        menu.append_section(None, misc_section)

        bottom_section = Gio.Menu()
        item = Gio.MenuItem.new(_("Keyboard Shortcuts"), "app.shortcuts")
        item.set_attribute_value("accel", GLib.Variant.new_string("<Control>question"))
        bottom_section.append_item(item)
        bottom_section.append(_("Preferences"), "app.preferences")
        bottom_section.append(_("About"), "app.about")
        menu.append_section(None, bottom_section)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_btn)

        # Content area: sidebar (220px) + editor panes
        content_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        content_paned.set_vexpand(True)
        outer_box.append(content_paned)

        # === LEFT: Sidebar (package list) — 220px, compact rows ===
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.set_size_request(220, -1)

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

        content_paned.set_start_child(sidebar_box)
        content_paned.set_resize_start_child(False)
        content_paned.set_shrink_start_child(False)
        content_paned.set_position(220)

        # === RIGHT: Editor area ===
        editor_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Package name banner above editor
        self._pkg_banner = Gtk.Label(label="", xalign=0)
        self._pkg_banner.add_css_class("title-4")
        self._pkg_banner.add_css_class("pkg-banner")
        self._pkg_banner.set_visible(False)
        editor_outer.append(self._pkg_banner)

        # Horizontal editor paned — Original (left) | Translation (right) — EQUAL SIZE
        self._editor_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._editor_paned.set_vexpand(True)

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
        self._editor_paned.set_start_child(left_box)

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
        self.trans_view.get_buffer().connect("changed", self._on_trans_buffer_changed)
        right_scroll.set_child(self.trans_view)
        right_box.append(right_scroll)
        self._editor_paned.set_end_child(right_box)

        editor_outer.append(self._editor_paned)

        # Button bar
        btn_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_bar.set_halign(Gtk.Align.END)
        btn_bar.set_margin_start(8)
        btn_bar.set_margin_end(8)
        btn_bar.set_margin_top(6)
        btn_bar.set_margin_bottom(2)

        self._auto_translate_btn = Gtk.Button(label=_("Auto-translate"))
        self._auto_translate_btn.set_icon_name("accessories-dictionary-symbolic")
        self._auto_translate_btn.set_tooltip_text(_("Translate using po-translate"))
        self._auto_translate_btn.set_sensitive(False)
        self._auto_translate_btn.connect("clicked", self._on_auto_translate)
        btn_bar.append(self._auto_translate_btn)

        self._add_queue_btn = Gtk.Button(label=_("Add to Queue"))
        self._add_queue_btn.set_tooltip_text(_("Add this translation to the send queue"))
        self._add_queue_btn.set_sensitive(False)
        self._add_queue_btn.connect("clicked", self._on_add_to_queue)
        btn_bar.append(self._add_queue_btn)

        self.submit_btn = Gtk.Button(label=_("Submit Now"))
        self.submit_btn.add_css_class("suggested-action")
        self.submit_btn.set_sensitive(False)
        self.submit_btn.connect("clicked", self._on_submit)
        btn_bar.append(self.submit_btn)

        editor_outer.append(btn_bar)
        content_paned.set_end_child(editor_outer)

        # === Status Bar (bottom) ===
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        status_bar.add_css_class("status-bar")

        self._status_counts = Gtk.Label(label="", xalign=0)
        self._status_counts.add_css_class("dim-label")
        status_bar.append(self._status_counts)

        self.status_label = Gtk.Label(label=_("Ready"), xalign=0.5, hexpand=True)
        self.status_label.add_css_class("dim-label")
        status_bar.append(self.status_label)

        self._status_right = Gtk.Label(label="", xalign=1)
        self._status_right.add_css_class("dim-label")
        status_bar.append(self._status_right)

        outer_box.append(status_bar)

        # Set equal paned position after window is realized
        self.connect("realize", self._on_realize_set_paned)

        # Load persisted queue
        self._queue = _load_queue()

        # Load initial data
        self._refresh_packages()
        self._update_queue_badge()
        self._update_status_bar()

    def _on_realize_set_paned(self, *_args):
        def set_pos():
            width = self._editor_paned.get_allocated_width()
            if width > 0:
                self._editor_paned.set_position(width // 2)
            return False
        GLib.idle_add(set_pos)

    # --- Status Bar ---

    def _update_status_bar(self):
        untranslated = len(self.packages)
        queue_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_READY)
        sent_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_SENT)
        self._status_counts.set_text(
            _("{untranslated} untranslated | {queue} in queue | {sent} submitted").format(
                untranslated=untranslated, queue=queue_count, sent=sent_count))

        lang = self._current_lang()
        lang_name = lang
        for code, name in DDTP_LANGUAGES:
            if code == lang:
                lang_name = name
                break
        login_status = _("logged in") if self._ddtss_logged_in else _("not logged in")
        self._status_right.set_text(f"{lang_name} | {self._completion_pct:.1f}% | DDTSS: {login_status}")

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

    def _on_sort_mode_changed(self, dropdown, *_args):
        idx = dropdown.get_selected()
        if 0 <= idx < len(self._sort_modes):
            self._sort_mode = self._sort_modes[idx]
            self.settings["sort_mode"] = self._sort_mode
            save_settings(self.settings)
            self._apply_sort_and_filter()

    def _on_filter_changed(self, dropdown, *_args):
        idx = dropdown.get_selected()
        if 0 <= idx < len(self._filter_values):
            self._status_filter = self._filter_values[idx]
            self._apply_sort_and_filter()

    def _get_sort_key(self, pkg):
        """Return sort key for a package based on current sort mode."""
        name = pkg.get("package", "").lower()
        if self._sort_mode == "status":
            status = self._pkg_ddtss_status.get(pkg.get("package", ""), "none")
            order = {"none": 0, "pending": 1, "reviewed_comment": 2, "reviewed_ok": 3}
            return (order.get(status, 0), name)
        elif self._sort_mode == "popcon":
            count = self._popcon_data.get(pkg.get("package", ""), 0)
            return (-count, name)  # Higher count first
        return name

    def _apply_sort_and_filter(self):
        """Re-sort and re-filter the package list."""
        pkgs = getattr(self, '_all_packages', self.packages)
        if not pkgs:
            return

        # Sort
        pkgs.sort(key=self._get_sort_key, reverse=not self._sort_ascending)

        # Filter by status
        if self._status_filter != "all":
            filtered = [p for p in pkgs if self._pkg_ddtss_status.get(p.get("package", ""), "none") == self._status_filter]
        else:
            filtered = pkgs

        # Apply max packages limit
        limit = self._get_max_packages()
        total = len(filtered)
        if limit > 0 and len(filtered) > limit:
            display = filtered[:limit]
            self.stats_label.set_text(_("{shown} of {total}").format(shown=limit, total=total))
        else:
            display = filtered
            self.stats_label.set_text(_("{n} packages").format(n=total))

        self._populate_list(display, update_stats=False)

    def _on_sort_clicked(self, btn):
        self._sort_ascending = not self._sort_ascending
        btn.set_icon_name(
            "view-sort-ascending-symbolic" if self._sort_ascending else "view-sort-descending-symbolic"
        )
        self._apply_sort_and_filter()

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

        # Fetch stats for completion %
        def do_stats():
            try:
                stats = fetch_ddtp_stats()
                lang_stats = stats.get("languages", {}).get(lang, {})
                active_pkgs = stats.get("active_packages", 0)
                active_trans = lang_stats.get("active_translations", 0)
                if active_pkgs > 0:
                    self._completion_pct = (active_trans / active_pkgs) * 100
                else:
                    self._completion_pct = 0.0
                GLib.idle_add(self._update_status_bar)
            except Exception:
                pass

        threading.Thread(target=do_stats, daemon=True).start()

    def _get_max_packages(self):
        return self.settings.get("max_packages", 500)

    def _on_packages_loaded(self, pkgs):
        total = len(pkgs)
        self._progress_bar.set_fraction(1.0)
        self._progress_bar.set_text(_("{n} packages loaded").format(n=total))
        GLib.timeout_add(1500, self._hide_progress)
        self._all_packages = pkgs
        self._apply_sort_and_filter()
        self._update_status_bar()

        # Fetch DDTSS statuses in background
        self._fetch_ddtss_statuses()
        # Fetch popcon data in background
        self._fetch_popcon_data()

    def _on_load_error(self, msg):
        self._progress_bar.set_visible(False)
        if "urlopen" in msg or "Connection refused" in msg or "timed out" in msg or "unreachable" in msg.lower():
            friendly = _("Could not connect to DDTP servers. Check your internet connection and try again.")
        else:
            friendly = _("Failed to load packages: {error}").format(error=msg)
        self.status_label.set_text(friendly)

    def _hide_progress(self):
        self._progress_bar.set_visible(False)
        return False

    def _fetch_ddtss_statuses(self):
        """Fetch DDTSS package statuses in background."""
        settings = load_settings()
        if not settings.get("fetch_ddtss_statuses", True):
            return
        if not settings.get("ddtss_alias"):
            return

        lang = self._current_lang()

        def do_fetch():
            try:
                client = DDTSSClient(lang=lang)
                if not client.is_logged_in():
                    client.login(settings["ddtss_alias"], settings.get("ddtss_password", ""))
                self._ddtss_logged_in = True
                statuses = client.get_package_statuses()

                status_map = {}
                # Pending translation = submitted but not yet reviewed
                for pkg_name in statuses.get("pending_translation", []):
                    status_map[pkg_name] = "pending"

                # Pending review
                for r in statuses.get("pending_review", []):
                    pkg_name = r["package"]
                    note = r.get("note", "")
                    if r.get("reviewed_by_you"):
                        status_map[pkg_name] = "reviewed_ok"
                    elif note:
                        status_map[pkg_name] = "reviewed_comment"
                    else:
                        status_map[pkg_name] = "pending"

                # Recently done
                for r in statuses.get("recently_reviewed", []):
                    status_map[r["package"]] = "reviewed_ok"

                GLib.idle_add(self._on_ddtss_statuses_loaded, status_map)
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text,
                              _("DDTSS status fetch failed: {e}").format(e=str(exc)))

        threading.Thread(target=do_fetch, daemon=True).start()

    def _on_ddtss_statuses_loaded(self, status_map):
        self._pkg_ddtss_status.update(status_map)
        self._apply_sort_and_filter()
        self._update_status_bar()

    def _fetch_popcon_data(self):
        """Fetch popcon data in background."""
        def do_fetch():
            try:
                data = fetch_popcon_data()
                GLib.idle_add(self._on_popcon_loaded, data)
            except Exception:
                pass

        threading.Thread(target=do_fetch, daemon=True).start()

    def _on_popcon_loaded(self, data):
        self._popcon_data = data
        # Re-sort if currently sorting by popcon
        if self._sort_mode == "popcon":
            self._apply_sort_and_filter()

    def _fetch_pkg_ddtss_data(self, package):
        """Fetch translation data for a specific package from DDTSS."""
        settings = load_settings()
        if not settings.get("ddtss_alias"):
            return

        lang = self._current_lang()

        def do_fetch():
            try:
                client = DDTSSClient(lang=lang)
                if not client.is_logged_in():
                    client.login(settings["ddtss_alias"], settings.get("ddtss_password", ""))
                status = self._pkg_ddtss_status.get(package, "none")
                if status in ("pending", "reviewed_comment", "reviewed_ok"):
                    data = client.get_review_page(package)
                else:
                    data = client.get_translate_page(package)
                GLib.idle_add(self._on_pkg_ddtss_data_loaded, package, data)
            except Exception:
                pass

        threading.Thread(target=do_fetch, daemon=True).start()

    def _on_pkg_ddtss_data_loaded(self, package, data):
        self._pkg_ddtss_data[package] = data
        # If this is the currently selected package, update the translation view
        if self.current_pkg and self.current_pkg.get("package") == package:
            trans_text = data.get("short_trans", "")
            if data.get("long_trans"):
                trans_text += "\n\n" + data["long_trans"]
            buf = self.trans_view.get_buffer()
            current = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
            if not current:
                buf.set_text(trans_text)

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

    def _get_status_icon(self, pkg_name):
        """Return a Gtk.Image status icon for a package, or None."""
        # Local session status takes priority
        if pkg_name in self._submitted_packages:
            icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            icon.set_tooltip_text(_("Submitted ✅"))
            icon.add_css_class("pkg-flag-submitted")
            return icon
        if pkg_name in self._error_packages:
            icon = Gtk.Image.new_from_icon_name("dialog-error-symbolic")
            icon.set_tooltip_text(_("Submission error ⚠️"))
            icon.add_css_class("pkg-flag-error")
            return icon
        if any(q.package == pkg_name and q.status == QueueItem.STATUS_READY for q in self._queue):
            icon = Gtk.Image.new_from_icon_name("mail-unread-symbolic")
            icon.set_tooltip_text(_("In queue 📬"))
            icon.add_css_class("pkg-flag-queued")
            return icon
        if pkg_name in self._modified_packages:
            icon = Gtk.Image.new_from_icon_name("document-edit-symbolic")
            icon.set_tooltip_text(_("Modified — not in queue 📝"))
            icon.add_css_class("pkg-flag-modified")
            return icon

        # DDTSS status icons
        ddtss_status = self._pkg_ddtss_status.get(pkg_name)
        if ddtss_status == "reviewed_ok":
            icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            icon.set_tooltip_text(_("Reviewed OK ✅"))
            icon.add_css_class("pkg-status-reviewed-ok")
            return icon
        if ddtss_status == "reviewed_comment":
            icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            icon.set_tooltip_text(_("Reviewed (with comments) 🟠"))
            icon.add_css_class("pkg-status-reviewed-comment")
            return icon
        if ddtss_status == "pending":
            icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            icon.set_tooltip_text(_("Submitted, not reviewed 🟡"))
            icon.add_css_class("pkg-status-pending")
            return icon

        # No translation (default blue)
        icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        icon.set_tooltip_text(_("Not translated 🔵"))
        icon.add_css_class("pkg-status-none")
        return icon

    def _populate_list(self, pkgs, update_stats=True):
        self.packages = pkgs
        self._clear_list()
        for pkg in pkgs:
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            row_box.add_css_class("compact-row")
            row_box.set_margin_start(6)
            row_box.set_margin_end(6)
            row_box.set_margin_top(2)
            row_box.set_margin_bottom(2)

            label = Gtk.Label(label=pkg["package"], xalign=0, hexpand=True)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            row_box.append(label)

            # Popcon count label
            popcon_count = self._popcon_data.get(pkg["package"], 0)
            if popcon_count > 0:
                pop_label = Gtk.Label(label=str(popcon_count))
                pop_label.add_css_class("dim-label")
                pop_label.set_tooltip_text(_("Popcon installs: {n}").format(n=popcon_count))
                row_box.append(pop_label)

            icon = self._get_status_icon(pkg["package"])
            row_box.append(icon)

            self.pkg_list.append(row_box)
        if update_stats:
            self.stats_label.set_text(_("{n} untranslated").format(n=len(pkgs)))
        self.status_label.set_text(_("Ready"))
        self._update_status_bar()

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

    def _on_trans_buffer_changed(self, buf):
        if self.current_pkg:
            pkg_name = self.current_pkg["package"]
            if pkg_name not in self._submitted_packages:
                self._modified_packages.add(pkg_name)

    def _refresh_pkg_list_flags(self):
        if self.packages:
            self._populate_list(self.packages, update_stats=False)

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
            first_child = child.get_first_child() if child else None
            text = first_child.get_text().lower() if first_child and hasattr(first_child, 'get_text') else ""
            row.set_visible(query in text)
            idx += 1

    def _on_pkg_selected(self, _listbox, row):
        if row is None:
            self.current_pkg = None
            self.submit_btn.set_sensitive(False)
            self._add_queue_btn.set_sensitive(False)
            self._auto_translate_btn.set_sensitive(False)
            self._pkg_banner.set_visible(False)
            return

        idx = row.get_index()
        if 0 <= idx < len(self.packages):
            pkg = self.packages[idx]
            self.current_pkg = pkg
            desc = pkg["short"]
            if pkg["long"]:
                desc += "\n\n" + pkg["long"]
            self.orig_view.get_buffer().set_text(desc)

            # If we have DDTSS data for this package, show the existing translation
            ddtss_data = self._pkg_ddtss_data.get(pkg["package"])
            if ddtss_data:
                trans_text = ddtss_data.get("short_trans", "")
                if ddtss_data.get("long_trans"):
                    trans_text += "\n\n" + ddtss_data["long_trans"]
                self.trans_view.get_buffer().set_text(trans_text)
            else:
                self.trans_view.get_buffer().set_text("")

            self.submit_btn.set_sensitive(True)
            self._add_queue_btn.set_sensitive(True)
            self._auto_translate_btn.set_sensitive(True)

            # Banner with status info
            ddtss_status = self._pkg_ddtss_status.get(pkg["package"], "none")
            status_labels = {
                "none": "",
                "pending": " — " + _("submitted, awaiting review"),
                "reviewed_comment": " — " + _("reviewed with comments"),
                "reviewed_ok": " — " + _("reviewed OK"),
            }
            popcon = self._popcon_data.get(pkg["package"], 0)
            banner = pkg["package"]
            if popcon:
                banner += f"  (popcon: {popcon})"
            banner += status_labels.get(ddtss_status, "")
            self._pkg_banner.set_text(banner)
            self._pkg_banner.set_visible(True)
            self.status_label.set_text(_("Editing: {pkg}").format(pkg=pkg["package"]))

            # If package has DDTSS status but we don't have the translation data yet, fetch it
            if ddtss_status in ("pending", "reviewed_comment", "reviewed_ok") and not ddtss_data:
                self._fetch_pkg_ddtss_data(pkg["package"])

    def _advance_to_next_package(self):
        if not self.packages:
            return
        row = self.pkg_list.get_selected_row()
        next_idx = (row.get_index() + 1) if row else 0
        if next_idx < len(self.packages):
            self._select_pkg_by_index(next_idx)

    def _go_to_prev_package(self):
        if not self.packages:
            return
        row = self.pkg_list.get_selected_row()
        if row and row.get_index() > 0:
            self._select_pkg_by_index(row.get_index() - 1)

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
        if not settings.get("ddtss_alias"):
            self.status_label.set_text(_("DDTSS not configured — open Preferences first"))
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
                client = DDTSSClient(lang=lang)
                if not client.is_logged_in():
                    client.login(settings["ddtss_alias"], settings.get("ddtss_password", ""))
                client.submit_translation(pkg["package"], short, long_text)
                self._ddtss_logged_in = True
                self._last_send_time = time.time()
                self._submitted_packages.add(pkg["package"])
                self._modified_packages.discard(pkg["package"])
                self._error_packages.discard(pkg["package"])
                GLib.idle_add(self._refresh_pkg_list_flags)
                GLib.idle_add(self._update_status_bar)
                GLib.idle_add(self._show_submit_result, pkg["package"], True, "")
                if settings.get("auto_advance", True):
                    GLib.idle_add(self._advance_to_next_package)
            except DDTSSAuthError as exc:
                self._error_packages.add(pkg["package"])
                GLib.idle_add(self._show_submit_result, pkg["package"], False,
                    _("Login failed: {e}").format(e=str(exc)))
            except DDTSSValidationError as exc:
                self._error_packages.add(pkg["package"])
                GLib.idle_add(self._show_submit_result, pkg["package"], False,
                    _("Validation error: {e}").format(e=str(exc)))
            except DDTSSLockedError as exc:
                self._error_packages.add(pkg["package"])
                GLib.idle_add(self._show_submit_result, pkg["package"], False,
                    _("Package locked: {e}").format(e=str(exc)))
            except Exception as exc:
                self._error_packages.add(pkg["package"])
                GLib.idle_add(self._show_submit_result, pkg["package"], False,
                    _("Error: {e}").format(e=str(exc)))
            GLib.idle_add(self.submit_btn.set_sensitive, True)

        threading.Thread(target=do_send, daemon=True).start()

    def _on_auto_translate(self, *_args):
        """Use po-translate to auto-translate the current package description."""
        if not self.current_pkg:
            return

        if not shutil.which("po-translate"):
            dialog = Adw.AlertDialog(
                heading=_("po-translate not found"),
                body=_("po-translate is not installed. Install it with:\n\n"
                       "pip install po-translate\n\n"
                       "or via your package manager."),
            )
            dialog.add_response("ok", "OK")
            dialog.present(self)
            return

        pkg = self.current_pkg
        lang = self._current_lang()
        orig_text = pkg["short"]
        if pkg["long"]:
            orig_text += "\n" + pkg["long"]

        self._auto_translate_btn.set_sensitive(False)
        self.status_label.set_text(_("Translating with po-translate…"))

        def do_translate():
            try:
                tmp_dir = tempfile.mkdtemp(prefix="ddtp-translate-")
                po_path = os.path.join(tmp_dir, "desc.po")

                lines = orig_text.split("\n")
                short = lines[0]
                long_parts = lines[1:] if len(lines) > 1 else []

                with open(po_path, "w", encoding="utf-8") as f:
                    f.write('msgid ""\nmsgstr ""\n')
                    f.write(f'"Content-Type: text/plain; charset=UTF-8\\n"\n')
                    f.write(f'"Language: {lang}\\n"\n\n')
                    f.write(f'msgid "{_po_escape(short)}"\nmsgstr ""\n\n')
                    if long_parts:
                        long_text = "\n".join(long_parts)
                        f.write('msgid ""\n')
                        for lp in long_text.split("\n"):
                            f.write(f'"{_po_escape(lp)}\\n"\n')
                        f.write('msgstr ""\n')

                result = subprocess.run(
                    ["po-translate", "--source", "en", "--target", lang, "-q", po_path],
                    capture_output=True, text=True, timeout=60,
                )

                if result.returncode != 0:
                    error = result.stderr.strip() or _("po-translate failed")
                    GLib.idle_add(self._auto_translate_done, None, error)
                else:
                    entries = _parse_po_entries(po_path)
                    translated_short = ""
                    translated_long = ""
                    if entries:
                        translated_short = entries[0][1] or entries[0][0]
                    if len(entries) > 1:
                        translated_long = entries[1][1] or entries[1][0]

                    translation = translated_short
                    if translated_long:
                        translation += "\n" + translated_long

                    GLib.idle_add(self._auto_translate_done, translation, None)

                shutil.rmtree(tmp_dir, ignore_errors=True)

            except subprocess.TimeoutExpired:
                GLib.idle_add(self._auto_translate_done, None, _("Translation timed out"))
            except Exception as exc:
                GLib.idle_add(self._auto_translate_done, None, str(exc))

        threading.Thread(target=do_translate, daemon=True).start()

    def _auto_translate_done(self, translation, error):
        self._auto_translate_btn.set_sensitive(True)
        if error:
            self.status_label.set_text(_("Auto-translate failed: {e}").format(e=error))
        elif translation:
            self.trans_view.get_buffer().set_text(translation)
            self.status_label.set_text(_("Auto-translated — review before submitting"))

    def _show_submit_result(self, package, success, error_msg):
        if success:
            self.status_label.set_text(_("Sent successfully!"))
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="✅ " + _("Translation submitted"),
                body=_("Package \"{pkg}\" was submitted successfully and is now pending review on DDTSS.").format(pkg=package),
            )
            dialog.add_response("ok", _("OK"))
            dialog.set_default_response("ok")
        else:
            self.status_label.set_text(error_msg)
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="❌ " + _("Submission failed"),
                body=_("Package \"{pkg}\" could not be submitted.\n\n{err}").format(pkg=package, err=error_msg),
            )
            dialog.add_response("ok", _("OK"))
            dialog.set_default_response("ok")
        dialog.present()

    # --- Queue management ---

    def _on_add_to_queue(self, *_args):
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

        for item in self._queue:
            if item.package == pkg["package"] and item.md5 == pkg["md5"]:
                item.short = short
                item.long_text = long_text
                item.status = QueueItem.STATUS_READY
                item.error_msg = ""
                _save_queue(self._queue)
                self._modified_packages.discard(pkg["package"])
                self._update_queue_badge()
                self._refresh_pkg_list_flags()
                self._update_status_bar()
                self.status_label.set_text(_("Updated {pkg} in queue").format(pkg=pkg["package"]))
                _log_event(f"Updated {pkg['package']} in queue")
                return

        self._queue.append(QueueItem(pkg["package"], pkg["md5"], short, long_text))
        _save_queue(self._queue)
        self._modified_packages.discard(pkg["package"])
        self._update_queue_badge()
        self._refresh_pkg_list_flags()
        self._update_status_bar()
        self.status_label.set_text(_("Added {pkg} to queue ({n} total)").format(
            pkg=pkg["package"], n=len(self._queue)))
        _log_event(f"Added {pkg['package']} to queue")

        settings = load_settings()
        if settings.get("auto_advance", True):
            self._advance_to_next_package()

    def _clear_sent(self, *_args):
        self._queue = [q for q in self._queue if q.status != QueueItem.STATUS_SENT]
        _save_queue(self._queue)
        self._update_queue_badge()
        self._update_status_bar()

    def _on_sort_queue(self, *_args):
        self._queue.sort(key=lambda q: q.package.lower())
        _save_queue(self._queue)
        self._update_queue_badge()

    def _clear_queue(self):
        self._queue = [q for q in self._queue if q.status == QueueItem.STATUS_SENDING]
        _save_queue(self._queue)
        self._update_queue_badge()
        self._update_status_bar()

    def _update_queue_badge(self):
        ready_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_READY)
        error_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_ERROR)
        sent_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_SENT)
        total = ready_count + error_count + sent_count
        if total > 0:
            parts = []
            if ready_count:
                parts.append(f"📬 {ready_count}")
            if sent_count:
                parts.append(f"✅ {sent_count}")
            if error_count:
                parts.append(f"⚠ {error_count}")
            self._queue_label.set_text(" ".join(parts) if parts else "📬 0")
            self._queue_label.set_visible(True)
        else:
            self._queue_label.set_visible(False)

    def _remove_queue_item(self, idx):
        if 0 <= idx < len(self._queue):
            removed = self._queue.pop(idx)
            _save_queue(self._queue)
            self._update_queue_badge()
            self._update_status_bar()
            self.status_label.set_text(_("Removed {pkg} from queue").format(pkg=removed.package))

    # --- Queue dialog ---

    def _on_show_queue_dialog(self, *_args):
        dialog = Adw.Window(
            transient_for=self,
            title=_("Send Queue"),
            default_width=600,
            default_height=500,
            modal=True,
        )

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        main_box.append(header)

        sort_btn = Gtk.Button(icon_name="view-sort-ascending-symbolic", tooltip_text=_("Sort queue"))
        sort_btn.connect("clicked", lambda b: self._on_sort_queue_and_refresh_dialog(dialog))
        header.pack_start(sort_btn)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        queue_list = Gtk.ListBox()
        queue_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(queue_list)
        main_box.append(scroll)

        self._populate_queue_list(queue_list, dialog)

        ready_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_READY)
        error_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_ERROR)
        sent_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_SENT)

        parts = []
        if ready_count:
            parts.append(_("{n} ready").format(n=ready_count))
        if sent_count:
            parts.append(_("{n} sent ✅").format(n=sent_count))
        if error_count:
            parts.append(_("{n} errors").format(n=error_count))

        info_label = Gtk.Label(
            label=", ".join(parts) if parts else _("Queue is empty"),
            xalign=0,
        )
        info_label.add_css_class("dim-label")
        info_label.set_margin_start(12)
        info_label.set_margin_end(12)
        info_label.set_margin_top(8)
        main_box.append(info_label)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_start(12)
        btn_box.set_margin_end(12)
        btn_box.set_margin_top(8)
        btn_box.set_margin_bottom(12)

        if sent_count > 0:
            clear_sent_btn = Gtk.Button(label=_("Clear sent ✅"))
            clear_sent_btn.connect("clicked", lambda b: (self._clear_sent(), dialog.close()))
            btn_box.append(clear_sent_btn)

        clear_btn = Gtk.Button(label=_("Clear all"))
        clear_btn.add_css_class("destructive-action")
        clear_btn.connect("clicked", lambda b: (self._clear_queue(), dialog.close()))
        btn_box.append(clear_btn)

        send_btn = Gtk.Button(label=_("Send All ({n})").format(n=ready_count))
        send_btn.add_css_class("suggested-action")
        send_btn.set_sensitive(ready_count > 0)
        send_btn.connect("clicked", lambda b: (dialog.close(), self._on_send_queue()))
        btn_box.append(send_btn)

        main_box.append(btn_box)
        dialog.set_content(main_box)
        dialog.present()

    def _populate_queue_list(self, queue_list, dialog):
        for i, item in enumerate(self._queue):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row_box.set_margin_start(8)
            row_box.set_margin_end(8)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)

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

            name_label = Gtk.Label(label=item.package, xalign=0, hexpand=True)
            name_label.set_ellipsize(Pango.EllipsizeMode.END)
            if item.error_msg:
                name_label.set_tooltip_text(item.error_msg)
            row_box.append(name_label)

            if item.status == QueueItem.STATUS_ERROR:
                err_label = Gtk.Label(label=_("Error"))
                err_label.add_css_class("error")
                row_box.append(err_label)

            if item.status != QueueItem.STATUS_SENDING:
                remove_btn = Gtk.Button(icon_name="user-trash-symbolic")
                remove_btn.add_css_class("flat")
                remove_btn.set_tooltip_text(_("Remove from queue"))
                remove_btn.connect("clicked", lambda b, idx=i: (
                    self._remove_queue_item(idx), dialog.close()))
                row_box.append(remove_btn)

            queue_list.append(row_box)

    def _on_sort_queue_and_refresh_dialog(self, dialog):
        self._on_sort_queue()
        dialog.close()
        self._on_show_queue_dialog()

    # --- Review dialog ---

    def _on_open_review(self, *_args):
        settings = load_settings()
        if not settings.get("ddtss_alias"):
            self.status_label.set_text(_("DDTSS not configured — open Preferences first"))
            return

        self.status_label.set_text(_("Loading reviews…"))

        def fetch():
            try:
                lang = self._current_lang()
                client = DDTSSClient(lang=lang)
                if not client.is_logged_in():
                    client.login(settings["ddtss_alias"], settings.get("ddtss_password", ""))
                self._ddtss_logged_in = True
                reviews = client.get_pending_reviews()
                GLib.idle_add(self._show_review_list, reviews, lang, settings)
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text, _("Error: {e}").format(e=str(exc)))

        threading.Thread(target=fetch, daemon=True).start()

    def _show_review_list(self, reviews, lang, settings):
        self.status_label.set_text("")

        pending = [r for r in reviews if not r.get("reviewed_by_you")]
        reviewed = [r for r in reviews if r.get("reviewed_by_you")]

        # Update review badge
        if pending:
            self._review_badge.set_text(f"🔍 {len(pending)}")
            self._review_badge.set_visible(True)
        else:
            self._review_badge.set_visible(False)

        dialog = Adw.Window(
            transient_for=self,
            title=_("Review Translations"),
            default_width=700,
            default_height=550,
            modal=True,
        )

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()

        if pending:
            accept_all_btn = Gtk.Button(label=_("Accept All ({n})").format(n=len(pending)))
            accept_all_btn.add_css_class("suggested-action")
            accept_all_btn.connect("clicked", lambda b: self._on_accept_all_reviews(
                pending, lang, settings, dialog))
            header.pack_end(accept_all_btn)

        main_box.append(header)

        self._current_reviews = reviews

        if not reviews:
            empty = Adw.StatusPage(
                icon_name="emblem-default-symbolic",
                title=_("No pending reviews"),
                description=_("There are no translations waiting for review."),
            )
            main_box.append(empty)
            dialog.set_content(main_box)
            dialog.present()
            return

        scroll = Gtk.ScrolledWindow(vexpand=True)
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")
        list_box.set_margin_start(12)
        list_box.set_margin_end(12)
        list_box.set_margin_top(8)
        scroll.set_child(list_box)
        main_box.append(scroll)

        if pending:
            header_row = Gtk.Label(label=_("Pending review ({n})").format(n=len(pending)), xalign=0)
            header_row.add_css_class("title-4")
            header_row.set_margin_start(16)
            header_row.set_margin_top(8)
            list_box.append(header_row)

            for r in pending:
                row = Adw.ActionRow(title=r["package"], subtitle=r.get("note", ""), activatable=True)
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda w, pkg=r["package"]: (
                    dialog.close(), self._open_review_detail(pkg, lang, settings)))
                list_box.append(row)

        if reviewed:
            header_row2 = Gtk.Label(label=_("Reviewed by you ({n})").format(n=len(reviewed)), xalign=0)
            header_row2.add_css_class("title-4")
            header_row2.set_margin_start(16)
            header_row2.set_margin_top(12)
            list_box.append(header_row2)

            for r in reviewed:
                row = Adw.ActionRow(title=r["package"], subtitle=r.get("note", ""), activatable=True)
                row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda w, pkg=r["package"]: (
                    dialog.close(), self._open_review_detail(pkg, lang, settings)))
                list_box.append(row)

        info = Gtk.Label(
            label=_("{pending} pending, {reviewed} reviewed by you").format(
                pending=len(pending), reviewed=len(reviewed)),
            xalign=0,
        )
        info.add_css_class("dim-label")
        info.set_margin_start(12)
        info.set_margin_bottom(8)
        main_box.append(info)

        dialog.set_content(main_box)
        dialog.present()

    def _on_accept_all_reviews(self, pending, lang, settings, parent_dialog):
        confirm = Adw.MessageDialog(
            transient_for=self,
            heading=_("Accept all {n} reviews?").format(n=len(pending)),
            body=_("This will accept all pending translations as-is. This cannot be undone."),
        )
        confirm.add_response("cancel", _("Cancel"))
        confirm.add_response("accept", _("Accept All"))
        confirm.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)
        confirm.set_default_response("cancel")

        def on_response(d, response):
            d.close()
            if response == "accept":
                parent_dialog.close()
                self._batch_accept_reviews(pending, lang, settings)

        confirm.connect("response", on_response)
        confirm.present()

    def _batch_accept_reviews(self, pending, lang, settings):
        self.status_label.set_text(_("Accepting {n} reviews…").format(n=len(pending)))

        def do_accept():
            accepted = 0
            errors = 0
            for r in pending:
                try:
                    client = DDTSSClient(lang=lang)
                    if not client.is_logged_in():
                        client.login(settings["ddtss_alias"], settings.get("ddtss_password", ""))
                    data = client.get_review_page(r["package"])
                    client.submit_review(
                        r["package"], action="accept",
                        short=data["short_trans"], long=data["long_trans"], comment="")
                    accepted += 1
                except Exception:
                    errors += 1
            msg = _("Accepted {accepted} reviews, {errors} errors").format(
                accepted=accepted, errors=errors)
            GLib.idle_add(self.status_label.set_text, msg)

        threading.Thread(target=do_accept, daemon=True).start()

    def _open_review_detail(self, package, lang, settings):
        self.status_label.set_text(_("Loading review for {pkg}…").format(pkg=package))

        def fetch():
            try:
                client = DDTSSClient(lang=lang)
                if not client.is_logged_in():
                    client.login(settings["ddtss_alias"], settings.get("ddtss_password", ""))
                data = client.get_review_page(package)
                GLib.idle_add(self._show_review_detail, data, lang, settings)
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text, _("Error: {e}").format(e=str(exc)))

        threading.Thread(target=fetch, daemon=True).start()

    def _show_review_detail(self, data, lang, settings):
        self.status_label.set_text("")

        dialog = Adw.Window(
            transient_for=self,
            title=_("Review: {pkg}").format(pkg=data["package"]),
            default_width=1100,
            default_height=800,
            modal=True,
        )

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()

        back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Back to list"))
        back_btn.connect("clicked", lambda b: (dialog.close(), self._on_open_review()))
        header.pack_start(back_btn)

        # Navigation buttons
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        prev_review_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Previous review (Ctrl+P)"))
        next_review_btn = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("Next review (Ctrl+N)"))

        def go_next_review(_b):
            reviews = getattr(self, '_current_reviews', [])
            pending = [r for r in reviews if not r.get("reviewed_by_you")]
            pkg_name = data["package"]
            idx = next((i for i, r in enumerate(pending) if r["package"] == pkg_name), -1)
            if idx >= 0 and idx + 1 < len(pending):
                dialog.close()
                self._open_review_detail(pending[idx + 1]["package"], lang, settings)

        def go_prev_review(_b):
            reviews = getattr(self, '_current_reviews', [])
            pending = [r for r in reviews if not r.get("reviewed_by_you")]
            pkg_name = data["package"]
            idx = next((i for i, r in enumerate(pending) if r["package"] == pkg_name), -1)
            if idx > 0:
                dialog.close()
                self._open_review_detail(pending[idx - 1]["package"], lang, settings)

        prev_review_btn.connect("clicked", go_prev_review)
        next_review_btn.connect("clicked", go_next_review)
        nav_box.append(prev_review_btn)
        nav_box.append(next_review_btn)
        header.pack_start(nav_box)

        main_box.append(header)

        # Keyboard shortcuts for review navigation
        key_ctrl = Gtk.EventControllerKey()

        def on_key_pressed(ctrl, keyval, keycode, state):
            if state & Gdk.ModifierType.CONTROL_MASK:
                if keyval == Gdk.KEY_n:
                    go_next_review(None)
                    return True
                elif keyval == Gdk.KEY_p:
                    go_prev_review(None)
                    return True
            return False

        key_ctrl.connect("key-pressed", on_key_pressed)
        dialog.add_controller(key_ctrl)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        scroll.set_child(content)
        main_box.append(scroll)

        if data.get("owner"):
            owner_label = Gtk.Label(label=_("Owner: {owner}").format(owner=data["owner"]), xalign=0)
            owner_label.add_css_class("dim-label")
            content.append(owner_label)

        orig_short_label = Gtk.Label(label=_("Original short description"), xalign=0)
        orig_short_label.add_css_class("title-4")
        content.append(orig_short_label)

        orig_short = Gtk.Label(label=data["short_orig"], xalign=0, selectable=True)
        orig_short.set_wrap(True)
        orig_short.add_css_class("monospace")
        content.append(orig_short)

        trans_short_label = Gtk.Label(label=_("Translated short description"), xalign=0)
        trans_short_label.add_css_class("title-4")
        content.append(trans_short_label)

        short_entry = Gtk.Entry()
        short_entry.set_text(data["short_trans"])
        content.append(short_entry)

        orig_long_label = Gtk.Label(label=_("Original long description"), xalign=0)
        orig_long_label.add_css_class("title-4")
        content.append(orig_long_label)

        orig_long = Gtk.Label(label=data["long_orig"], xalign=0, selectable=True)
        orig_long.set_wrap(True)
        orig_long.add_css_class("monospace")
        content.append(orig_long)

        trans_long_label = Gtk.Label(label=_("Translated long description"), xalign=0)
        trans_long_label.add_css_class("title-4")
        content.append(trans_long_label)

        long_buf = Gtk.TextBuffer()
        long_buf.set_text(data["long_trans"])
        long_view = Gtk.TextView(buffer=long_buf, editable=True, wrap_mode=Gtk.WrapMode.WORD)
        long_view.add_css_class("monospace")
        long_view.set_size_request(-1, 120)
        long_frame = Gtk.Frame()
        long_frame.set_child(long_view)
        content.append(long_frame)

        comment_label = Gtk.Label(label=_("Comment (optional)"), xalign=0)
        comment_label.add_css_class("title-4")
        content.append(comment_label)

        comment_buf = Gtk.TextBuffer()
        comment_buf.set_text(data.get("comment", ""))
        comment_view = Gtk.TextView(buffer=comment_buf, editable=True, wrap_mode=Gtk.WrapMode.WORD)
        comment_view.set_size_request(-1, 60)
        comment_frame = Gtk.Frame()
        comment_frame.set_child(comment_view)
        content.append(comment_frame)

        if data.get("log"):
            log_label = Gtk.Label(label=_("Log"), xalign=0)
            log_label.add_css_class("title-4")
            content.append(log_label)

            log_text = Gtk.Label(label=data["log"], xalign=0, selectable=True)
            log_text.add_css_class("monospace")
            log_text.add_css_class("dim-label")
            content.append(log_text)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(12)
        btn_box.set_margin_bottom(12)

        def get_comment():
            return comment_buf.get_text(comment_buf.get_start_iter(), comment_buf.get_end_iter(), False).strip()

        def get_short():
            return short_entry.get_text().strip()

        def get_long():
            return long_buf.get_text(long_buf.get_start_iter(), long_buf.get_end_iter(), False)

        def do_review(action):
            for btn in (accept_btn, changes_btn, comment_btn):
                btn.set_sensitive(False)
            self.status_label.set_text(_("Submitting review…"))

            def submit():
                try:
                    client = DDTSSClient(lang=lang)
                    if not client.is_logged_in():
                        client.login(settings["ddtss_alias"], settings.get("ddtss_password", ""))
                    client.submit_review(
                        data["package"],
                        action=action,
                        short=get_short(),
                        long=get_long(),
                        comment=get_comment(),
                    )
                    GLib.idle_add(self._show_review_result, data["package"], action, True, "")
                    GLib.idle_add(dialog.close)
                except Exception as exc:
                    GLib.idle_add(self._show_review_result, data["package"], action, False, str(exc))
                    for btn in (accept_btn, changes_btn, comment_btn):
                        GLib.idle_add(btn.set_sensitive, True)

            threading.Thread(target=submit, daemon=True).start()

        comment_btn = Gtk.Button(label=_("Comment only"))
        comment_btn.set_tooltip_text(_("Save comment without accepting"))
        comment_btn.connect("clicked", lambda b: do_review("comment"))
        btn_box.append(comment_btn)

        changes_btn = Gtk.Button(label=_("Accept with changes"))
        changes_btn.add_css_class("suggested-action")
        changes_btn.set_tooltip_text(_("Accept translation with your modifications — restarts review"))
        changes_btn.connect("clicked", lambda b: do_review("changes"))
        btn_box.append(changes_btn)

        accept_btn = Gtk.Button(label="✅ " + _("Accept as is"))
        accept_btn.add_css_class("suggested-action")
        accept_btn.set_tooltip_text(_("Accept translation without changes"))
        accept_btn.connect("clicked", lambda b: do_review("accept"))
        btn_box.append(accept_btn)

        content.append(btn_box)
        dialog.set_content(main_box)
        dialog.present()

    def _show_review_result(self, package, action, success, error_msg):
        if success:
            if action == "accept":
                msg = _("Translation for \"{pkg}\" accepted.")
            elif action == "changes":
                msg = _("Translation for \"{pkg}\" accepted with changes (review restarted).")
            else:
                msg = _("Comment saved for \"{pkg}\".")
            self.status_label.set_text("✅ " + msg.format(pkg=package))
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="✅ " + _("Review submitted"),
                body=msg.format(pkg=package),
            )
        else:
            self.status_label.set_text("❌ " + error_msg)
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="❌ " + _("Review failed"),
                body=_("Could not submit review for \"{pkg}\".\n\n{err}").format(
                    pkg=package, err=error_msg),
            )
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.present()

    # --- Statistics dialog ---

    def _on_show_stats(self, *_args):
        self.status_label.set_text(_("Fetching statistics…"))

        def do_fetch():
            try:
                stats = fetch_ddtp_stats()
                GLib.idle_add(self._show_stats_dialog, stats)
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text,
                              _("Failed to fetch statistics: {e}").format(e=str(exc)))

        threading.Thread(target=do_fetch, daemon=True).start()

    def _show_stats_dialog(self, stats):
        self.status_label.set_text(_("Ready"))
        lang = self._current_lang()
        lang_stats = stats.get("languages", {}).get(lang, {})
        total_pkgs = stats.get("total_packages", 0)
        active_pkgs = stats.get("active_packages", 0)

        active_trans = lang_stats.get("active_translations", 0)
        prev_trans = lang_stats.get("previously_translated", 0)
        total_trans = lang_stats.get("total_translations", 0)

        pct = (active_trans / active_pkgs) * 100 if active_pkgs > 0 else 0

        lang_name = lang
        for code, name in DDTP_LANGUAGES:
            if code == lang:
                lang_name = name
                break

        body = _(
            "Language: {lang} ({code})\n\n"
            "Active translations: {active}\n"
            "Previously translated: {prev}\n"
            "Total translations (all time): {total}\n\n"
            "Active packages in Debian: {active_pkgs}\n"
            "Total packages: {total_pkgs}\n\n"
            "Completion: {pct:.1f}% of active packages"
        ).format(
            lang=lang_name, code=lang,
            active=active_trans, prev=prev_trans, total=total_trans,
            active_pkgs=active_pkgs, total_pkgs=total_pkgs,
            pct=pct,
        )

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("DDTP Statistics"),
            body=body,
        )
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.connect("response", lambda d, r: d.close())
        dialog.present()

    # --- Lint ---

    def _on_lint(self, *_args):
        import shutil
        import subprocess
        import tempfile

        if not self.current_pkg:
            self.status_label.set_text(_("No package selected"))
            return

        buf = self.trans_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text:
            self.status_label.set_text(_("Translation is empty — nothing to lint"))
            return

        if not shutil.which("l10n-lint"):
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading=_("l10n-lint not found"),
                body=_(
                    "l10n-lint is not installed.\n\n"
                    "Install it with:\n"
                    "  pip install l10n-lint\n\n"
                    "Or on Debian/Ubuntu:\n"
                    "  apt install l10n-lint"
                ),
            )
            dialog.add_response("close", _("Close"))
            dialog.connect("response", lambda d, r: d.close())
            dialog.present()
            return

        pkg = self.current_pkg
        lang = self._current_lang()
        lines = text.split("\n", 1)
        short_trans = lines[0]
        long_trans = lines[1].strip() if len(lines) > 1 else ""

        po_content = (
            f'msgid "{self._po_escape(pkg["short"])}"\n'
            f'msgstr "{self._po_escape(short_trans)}"\n'
        )
        if pkg["long"] and long_trans:
            po_content += (
                f'\nmsgctxt "long:{pkg["package"]}"\n'
                f'msgid {self._po_escape_multiline(pkg["long"])}\n'
                f'msgstr {self._po_escape_multiline(long_trans)}\n'
            )

        self.status_label.set_text(_("Running l10n-lint…"))

        def do_lint():
            try:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".po", delete=False, encoding="utf-8") as f:
                    f.write(f'msgid ""\nmsgstr ""\n"Language: {lang}\\n"\n'
                            f'"Content-Type: text/plain; charset=UTF-8\\n"\n\n')
                    f.write(po_content)
                    tmp_path = f.name

                result = subprocess.run(
                    ["l10n-lint", "--format", "text", tmp_path],
                    capture_output=True, text=True, timeout=30,
                )
                os.unlink(tmp_path)
                output = (result.stdout + result.stderr).strip()
                GLib.idle_add(self._show_lint_result, output, result.returncode)
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text,
                              _("Lint error: {e}").format(e=str(exc)))

        threading.Thread(target=do_lint, daemon=True).start()

    def _show_lint_result(self, output, returncode):
        self.status_label.set_text(_("Ready"))
        if returncode == 0 and not output:
            heading = _("Lint: No issues found ✓")
            body = _("The translation passed all lint checks.")
        elif returncode == 0:
            heading = _("Lint: Passed with notes")
            body = output
        else:
            heading = _("Lint: Issues found")
            body = output if output else _("l10n-lint reported errors (exit code {c}).").format(c=returncode)

        _log_event(f"Lint result for {self.current_pkg.get('package', '?')}: exit={returncode}")

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=heading,
            body=body,
        )
        dialog.add_response("close", _("Close"))
        dialog.connect("response", lambda d, r: d.close())
        dialog.present()

    # --- Batch send ---

    def _on_send_queue(self, *_args):
        ready = [q for q in self._queue if q.status == QueueItem.STATUS_READY]
        if not ready:
            return

        settings = load_settings()
        if not settings.get("ddtss_alias"):
            self.status_label.set_text(_("DDTSS not configured — open Preferences first"))
            return

        total = len(ready)
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Submit {n} translations via DDTSS?").format(n=total),
            body=_(
                "You are about to submit {n} translation(s) to the DDTSS web interface.\n\n"
                "• Translations are submitted directly via HTTP — no delay needed\n"
                "• Successfully submitted packages are marked with ✅\n"
                "• You can cancel the process at any time\n"
                "• Any errors will be shown per package"
            ).format(n=total),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("send", _("Submit All"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")

        def on_response(d, response):
            d.close()
            if response == "send":
                self._start_batch_send()

        dialog.connect("response", on_response)
        dialog.present()

    def _start_batch_send(self):
        self._batch_running = True
        self._batch_cancel = False

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

        self._batch_heading = Gtk.Label(label=_("Sending translations…"))
        self._batch_heading.add_css_class("title-2")
        dialog_box.append(self._batch_heading)

        self._batch_progress = Gtk.ProgressBar()
        self._batch_progress.set_show_text(True)
        dialog_box.append(self._batch_progress)

        self._batch_current = Gtk.Label(label="", xalign=0)
        self._batch_current.add_css_class("dim-label")
        dialog_box.append(self._batch_current)

        log_scroll = Gtk.ScrolledWindow(vexpand=True)
        self._batch_log_buf = Gtk.TextBuffer()
        log_view = Gtk.TextView(buffer=self._batch_log_buf, editable=False, wrap_mode=Gtk.WrapMode.WORD)
        log_view.add_css_class("monospace")
        log_scroll.set_child(log_view)
        dialog_box.append(log_scroll)

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

        threading.Thread(target=self._batch_send_worker, daemon=True).start()

    def _batch_log(self, text):
        def do_log():
            end = self._batch_log_buf.get_end_iter()
            self._batch_log_buf.insert(end, text + "\n")
        GLib.idle_add(do_log)

    def _on_batch_cancel(self, *_args):
        self._batch_cancel = True
        self._batch_cancel_btn.set_sensitive(False)
        self._batch_log(_("⏸ Cancelling…"))

    def _batch_send_worker(self):
        settings = load_settings()
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
            GLib.idle_add(self._update_queue_badge)
            GLib.idle_add(self._batch_current.set_text,
                          _("Sending: {pkg} ({i}/{total})").format(pkg=item.package, i=i + 1, total=total))
            GLib.idle_add(self._batch_progress.set_fraction, (i + 0.5) / total)
            GLib.idle_add(self._batch_progress.set_text, f"{i + 1}/{total}")

            try:
                client = DDTSSClient(lang=lang)
                if not client.is_logged_in():
                    client.login(settings.get("ddtss_alias", ""), settings.get("ddtss_password", ""))
                client.submit_translation(item.package, item.short, item.long_text)
                self._ddtss_logged_in = True
                item.status = QueueItem.STATUS_SENT
                sent += 1
                self._submitted_packages.add(item.package)
                self._modified_packages.discard(item.package)
                self._error_packages.discard(item.package)
                self._batch_log(f"✅ {item.package}")
            except Exception as exc:
                item.status = QueueItem.STATUS_ERROR
                item.error_msg = str(exc)
                errors += 1
                self._error_packages.add(item.package)
                self._batch_log(f"❌ {item.package}: {exc}")

            _save_queue(self._queue)
            _log_event(f"Batch: {item.package} -> {item.status}")
            GLib.idle_add(self._update_queue_badge)
            GLib.idle_add(self._batch_progress.set_fraction, (i + 1) / total)

        _save_queue(self._queue)

        self._batch_running = False
        summary = _("Done! {sent} sent, {errors} errors out of {total}").format(
            sent=sent, errors=errors, total=total)
        self._batch_log(f"\n{summary}")
        _log_event(summary)
        if errors == 0:
            heading = "✅ " + _("All {n} translations submitted").format(n=sent)
        elif sent == 0:
            heading = "❌ " + _("All {n} submissions failed").format(n=errors)
        else:
            heading = "⚠️ " + _("{sent} sent, {errors} failed").format(sent=sent, errors=errors)
        GLib.idle_add(self._batch_heading.set_text, heading)
        GLib.idle_add(self._batch_current.set_text, summary)
        GLib.idle_add(self._batch_cancel_btn.set_sensitive, False)
        GLib.idle_add(self._batch_close_btn.set_sensitive, True)
        GLib.idle_add(self._update_queue_badge)
        GLib.idle_add(self._refresh_pkg_list_flags)
        GLib.idle_add(self._update_status_bar)
        GLib.idle_add(self.status_label.set_text, summary)

    # --- Close confirmation ---

    def _on_close_request(self, *_args):
        ready_count = sum(1 for q in self._queue if q.status == QueueItem.STATUS_READY)
        modified_count = len(self._modified_packages)

        warnings = []
        if ready_count > 0:
            warnings.append(_("{n} translation(s) in queue not yet submitted").format(n=ready_count))
        if modified_count > 0:
            warnings.append(_("{n} translation(s) modified but not added to queue").format(n=modified_count))

        if not warnings:
            return False

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Quit DDTP Translate?"),
            body=_("You have unsaved work:\n\n• {items}\n\nThis will be lost if you quit.").format(
                items="\n• ".join(warnings)),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("quit", _("Quit anyway"))
        dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(d, response):
            d.close()
            if response == "quit":
                self.get_application().quit()

        dialog.connect("response", on_response)
        dialog.present()
        return True

    # --- PO Export with Filter Dialog ---

    def _on_export_po(self, *_args):
        export_pkgs = getattr(self, '_all_packages', self.packages)
        if not export_pkgs:
            self.status_label.set_text(_("No packages loaded to export"))
            return

        filter_dialog = Adw.Window(
            transient_for=self,
            title=_("Export PO — Filter"),
            default_width=450,
            default_height=400,
            modal=True,
        )

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        fheader = Adw.HeaderBar()
        main_box.append(fheader)

        content = Adw.PreferencesPage()

        filter_group = Adw.PreferencesGroup(
            title=_("Filter Options"),
            description=_("Narrow down which packages to export."),
        )

        max_spin = Adw.SpinRow.new_with_range(0, len(export_pkgs), 10)
        max_spin.set_title(_("Max number of packages (0 = all)"))
        max_spin.set_value(0)
        filter_group.add(max_spin)

        letter_row = Adw.EntryRow(title=_("Starting letter"))
        letter_row.set_text("")
        filter_group.add(letter_row)

        regex_row = Adw.EntryRow(title=_("Regex filter"))
        regex_row.set_text("")
        filter_group.add(regex_row)

        content.add(filter_group)

        preview_group = Adw.PreferencesGroup()
        preview_label = Gtk.Label(
            label=_("Matching: {x} of {y} packages").format(x=len(export_pkgs), y=len(export_pkgs)),
            xalign=0,
        )
        preview_label.add_css_class("dim-label")
        preview_label.set_margin_start(12)
        preview_label.set_margin_top(8)
        preview_group.add(preview_label)
        content.add(preview_group)

        main_box.append(content)

        def compute_filtered():
            pkgs = export_pkgs
            letter = letter_row.get_text().strip().lower()
            if letter:
                pkgs = [p for p in pkgs if p["package"].lower().startswith(letter)]
            regex_text = regex_row.get_text().strip()
            if regex_text:
                try:
                    pattern = re.compile(regex_text)
                    pkgs = [p for p in pkgs if pattern.search(p["package"])]
                except re.error:
                    pass
            max_n = int(max_spin.get_value())
            if max_n > 0:
                pkgs = pkgs[:max_n]
            return pkgs

        def update_preview(*_args):
            filtered = compute_filtered()
            preview_label.set_text(
                _("Matching: {x} of {y} packages").format(x=len(filtered), y=len(export_pkgs)))

        max_spin.connect("notify::value", update_preview)
        letter_row.connect("changed", update_preview)
        regex_row.connect("changed", update_preview)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_start(12)
        btn_box.set_margin_end(12)
        btn_box.set_margin_top(8)
        btn_box.set_margin_bottom(12)

        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect("clicked", lambda b: filter_dialog.close())
        btn_box.append(cancel_btn)

        do_export_btn = Gtk.Button(label=_("Export"))
        do_export_btn.add_css_class("suggested-action")
        do_export_btn.connect("clicked", lambda b: (
            setattr(self, '_export_pkgs_pending', compute_filtered()),
            filter_dialog.close(),
            self._open_export_save_dialog(),
        ))
        btn_box.append(do_export_btn)

        main_box.append(btn_box)
        filter_dialog.set_content(main_box)
        filter_dialog.present()

    def _open_export_save_dialog(self):
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
        export_pkgs = getattr(self, '_export_pkgs_pending', self.packages)
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

    # --- PO Import with Review Window ---

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

        self._show_import_review(translations)

    def _show_import_review(self, translations):
        """Show import review window with lint results."""
        import shutil

        review_win = Adw.Window(
            transient_for=self,
            title=_("Import Review — {n} translations").format(n=len(translations)),
            default_width=800,
            default_height=600,
            modal=True,
        )

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        main_box.append(header)

        content_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        content_paned.set_vexpand(True)

        # Left: list
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left_box.set_size_request(300, -1)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        scroll.set_child(list_box)
        left_box.append(scroll)

        content_paned.set_start_child(left_box)

        # Right: detail
        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        detail_box.set_margin_start(12)
        detail_box.set_margin_end(12)
        detail_box.set_margin_top(8)
        detail_box.set_margin_bottom(8)

        detail_title = Gtk.Label(label=_("Select a translation to preview"), xalign=0)
        detail_title.add_css_class("title-4")
        detail_box.append(detail_title)

        detail_scroll = Gtk.ScrolledWindow(vexpand=True)
        detail_view = Gtk.TextView(editable=True, wrap_mode=Gtk.WrapMode.WORD)
        detail_view.add_css_class("monospace")
        detail_scroll.set_child(detail_view)
        detail_box.append(detail_scroll)

        content_paned.set_end_child(detail_box)
        main_box.append(content_paned)

        has_lint = shutil.which("l10n-lint") is not None

        for i, (pkg, md5, short, long_text) in enumerate(translations):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row_box.set_margin_start(8)
            row_box.set_margin_end(8)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)

            lint_icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
            lint_icon.set_tooltip_text(_("Checking…"))
            row_box.append(lint_icon)

            name_label = Gtk.Label(label=pkg, xalign=0, hexpand=True)
            name_label.set_ellipsize(Pango.EllipsizeMode.END)
            row_box.append(name_label)

            preview = short[:40] + "…" if len(short) > 40 else short
            preview_lbl = Gtk.Label(label=preview, xalign=1)
            preview_lbl.add_css_class("dim-label")
            preview_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            row_box.append(preview_lbl)

            list_box.append(row_box)

        def on_row_selected(lb, row):
            if row is None:
                return
            idx = row.get_index()
            if 0 <= idx < len(translations):
                pkg, md5, short, long_text = translations[idx]
                detail_title.set_text(pkg)
                text = short
                if long_text:
                    text += "\n\n" + long_text
                detail_view.get_buffer().set_text(text)

        list_box.connect("row-selected", on_row_selected)

        if has_lint:
            def run_lint_all():
                import subprocess
                import tempfile
                lang = self._current_lang()
                for i, (pkg, md5, short, long_text) in enumerate(translations):
                    try:
                        po_content = (
                            f'msgid ""\nmsgstr ""\n"Language: {lang}\\n"\n'
                            f'"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
                            f'msgid "{self._po_escape(short)}"\n'
                            f'msgstr "{self._po_escape(short)}"\n'
                        )
                        with tempfile.NamedTemporaryFile(mode="w", suffix=".po", delete=False, encoding="utf-8") as f:
                            f.write(po_content)
                            tmp_path = f.name
                        result = subprocess.run(
                            ["l10n-lint", "--format", "text", tmp_path],
                            capture_output=True, text=True, timeout=10,
                        )
                        os.unlink(tmp_path)
                        lint_ok = result.returncode == 0
                        GLib.idle_add(self._update_import_lint_icon, list_box, i, lint_ok)
                    except Exception:
                        GLib.idle_add(self._update_import_lint_icon, list_box, i, True)

            threading.Thread(target=run_lint_all, daemon=True).start()
        else:
            info_label = Gtk.Label(
                label=_("l10n-lint not installed — skipping lint checks"),
                xalign=0,
            )
            info_label.add_css_class("dim-label")
            info_label.set_margin_start(12)
            info_label.set_margin_bottom(4)
            main_box.append(info_label)
            for i in range(len(translations)):
                GLib.idle_add(self._update_import_lint_icon, list_box, i, True)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_start(12)
        btn_box.set_margin_end(12)
        btn_box.set_margin_top(8)
        btn_box.set_margin_bottom(12)

        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect("clicked", lambda b: review_win.close())
        btn_box.append(cancel_btn)

        add_selected_btn = Gtk.Button(label=_("Add Selected to Queue"))
        add_selected_btn.connect("clicked", lambda b: (
            self._import_selected_to_queue(list_box, translations),
            review_win.close(),
        ))
        btn_box.append(add_selected_btn)

        add_all_btn = Gtk.Button(label=_("Add All to Queue"))
        add_all_btn.add_css_class("suggested-action")
        add_all_btn.connect("clicked", lambda b: (
            self._import_all_to_queue(translations),
            review_win.close(),
        ))
        btn_box.append(add_all_btn)

        main_box.append(btn_box)
        review_win.set_content(main_box)
        review_win.present()

    def _update_import_lint_icon(self, list_box, idx, lint_ok):
        row = list_box.get_row_at_index(idx)
        if row is None:
            return
        row_box = row.get_child()
        if row_box is None:
            return
        icon = row_box.get_first_child()
        if icon and isinstance(icon, Gtk.Image):
            if lint_ok:
                icon.set_from_icon_name("emblem-ok-symbolic")
                icon.set_tooltip_text(_("Lint OK ✅"))
                icon.add_css_class("pkg-flag-submitted")
            else:
                icon.set_from_icon_name("dialog-warning-symbolic")
                icon.set_tooltip_text(_("Lint issues ⚠️"))
                icon.add_css_class("pkg-flag-modified")

    def _import_selected_to_queue(self, list_box, translations):
        selected_rows = list_box.get_selected_rows()
        added = 0
        updated = 0
        for row in selected_rows:
            idx = row.get_index()
            if 0 <= idx < len(translations):
                pkg, md5, short, long_text = translations[idx]
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
        _save_queue(self._queue)
        self._update_queue_badge()
        self._update_status_bar()
        self.status_label.set_text(
            _("Imported {added} new, {updated} updated — {total} in queue").format(
                added=added, updated=updated, total=len(self._queue)))

    def _import_all_to_queue(self, translations):
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

        _save_queue(self._queue)
        self._update_queue_badge()
        self._update_status_bar()
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

        return [(p, m, s, l) for p, m, s, l in translations if s]

    def _po_unescape_joined(self, parts):
        raw = ''.join(parts)
        return raw.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')


# --- Application ---

class DDTPTranslateApp(Adw.Application):
    """Main application."""

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

        self.create_action("preferences", self._on_preferences)
        self.create_action("about", self._on_about)
        self.create_action("export-po", self._on_export_po_action)
        self.create_action("import-po", self._on_import_po_action)
        self.create_action("clear-queue", self._on_clear_queue_action)
        self.create_action("show-queue", self._on_show_queue_action)
        self.create_action("stats", self._on_stats_action)
        self.create_action("review", self._on_review_action)
        self.create_action("shortcuts", self._on_shortcuts)
        self.create_action("submit-now", self._on_submit_now_action)
        self.create_action("add-to-queue", self._on_add_to_queue_action)
        self.create_action("next-package", self._on_next_package_action)
        self.create_action("prev-package", self._on_prev_package_action)
        self.create_action("lint", self._on_lint_action)
        self.create_action("refresh", self._on_refresh_action)
        self.create_action("quit", self._on_quit_action)

        # Keyboard shortcuts
        self.set_accels_for_action("app.submit-now", ["<Control>Return"])
        self.set_accels_for_action("app.add-to-queue", ["<Control><Shift>Return"])
        self.set_accels_for_action("app.next-package", ["<Control>n"])
        self.set_accels_for_action("app.prev-package", ["<Control>p"])
        self.set_accels_for_action("app.lint", ["<Control>l"])
        self.set_accels_for_action("app.refresh", ["F5"])
        self.set_accels_for_action("app.quit", ["<Control>q"])

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
                "5. Send the queue — translations go to DDTSS\n\n"
                "Tip: Export as PO, translate in your favorite editor, "
                "then import back to queue many at once.\n\n"
                "Before submitting, configure your DDTSS account in Preferences."
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

    # --- Action handlers ---

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

    def _on_show_queue_action(self, *_args):
        win = self.props.active_window
        if win:
            win._on_show_queue_dialog()

    def _on_review_action(self, *_args):
        win = self.props.active_window
        if win:
            win._on_open_review()

    def _on_stats_action(self, *_args):
        win = self.props.active_window
        if win:
            win._on_show_stats()

    def _on_submit_now_action(self, *_args):
        win = self.props.active_window
        if win:
            win._on_submit()

    def _on_add_to_queue_action(self, *_args):
        win = self.props.active_window
        if win:
            win._on_add_to_queue()

    def _on_next_package_action(self, *_args):
        win = self.props.active_window
        if win:
            win._advance_to_next_package()

    def _on_prev_package_action(self, *_args):
        win = self.props.active_window
        if win:
            win._go_to_prev_package()

    def _on_lint_action(self, *_args):
        win = self.props.active_window
        if win:
            win._on_lint()

    def _on_refresh_action(self, *_args):
        win = self.props.active_window
        if win:
            win._on_refresh()

    def _on_quit_action(self, *_args):
        self.quit()

    def _on_preferences(self, *_args):
        win = PreferencesWindow(self.props.active_window)
        win.present()

    def _on_about(self, *_args):
        about = Adw.AboutDialog(
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
            translator_credits=_("Translate this app: https://www.transifex.com/danielnylander/ddtp-translate/"),
            comments=_("Translate Debian package descriptions via DDTP"),
        )
        about.present(self.props.active_window)

    def _on_shortcuts(self, *_args):
        """Show the keyboard shortcuts window."""
        shortcuts_win = Gtk.ShortcutsWindow(
            transient_for=self.props.active_window,
            modal=True,
        )

        section = Gtk.ShortcutsSection(section_name="shortcuts", title=_("Shortcuts"))
        section.set_visible(True)

        trans_group = Gtk.ShortcutsGroup(title=_("Translation"))
        trans_group.set_visible(True)
        for title, accel in [
            (_("Submit current translation"), "<Control>Return"),
            (_("Add to queue"), "<Control><Shift>Return"),
            (_("Lint translation"), "<Control>l"),
            (_("Refresh packages"), "F5"),
        ]:
            sc = Gtk.ShortcutsShortcut(title=title, accelerator=accel)
            sc.set_visible(True)
            trans_group.append(sc)
        section.append(trans_group)

        nav_group = Gtk.ShortcutsGroup(title=_("Navigation"))
        nav_group.set_visible(True)
        for title, accel in [
            (_("Next package"), "<Control>n"),
            (_("Previous package"), "<Control>p"),
            (_("Focus search"), "<Control>f"),
        ]:
            sc = Gtk.ShortcutsShortcut(title=title, accelerator=accel)
            sc.set_visible(True)
            nav_group.append(sc)
        section.append(nav_group)

        app_group = Gtk.ShortcutsGroup(title=_("Application"))
        app_group.set_visible(True)
        for title, accel in [
            (_("Quit"), "<Control>q"),
            (_("Keyboard shortcuts"), "<Control>question"),
        ]:
            sc = Gtk.ShortcutsShortcut(title=title, accelerator=accel)
            sc.set_visible(True)
            app_group.append(sc)
        section.append(app_group)

        shortcuts_win.add_section(section)
        shortcuts_win.present()


def main():
    app = DDTPTranslateApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
