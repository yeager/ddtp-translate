# Changelog

## 0.8.0 (2026-02-17)

### Added
- **Three-panel layout** — sidebar (220px), original (left), translation (right) with equal-size horizontal panes
- **Package name banner** — displayed above the editor, not in sidebar
- **Permanent status bar** — left: untranslated/queue/submitted counts; center: action status; right: language, completion %, DDTSS login status
- **PO Export filter dialog** — filter by max packages, starting letter, regex; live preview of matching count
- **PO Import review window** — preview imported translations, auto-lint each entry, add all/selected to queue
- **Accept All reviews** — batch accept all pending reviews with confirmation dialog
- **Review navigation** — Ctrl+N/Ctrl+P to navigate between reviews, prev/next buttons in review detail
- **Review count badge** — shows pending review count in header bar
- **Keyboard Shortcuts window** — accessible from hamburger menu, organized by group
- **Keyboard shortcuts** — Ctrl+Return (submit), Ctrl+Shift+Return (queue), Ctrl+N/P (navigate), Ctrl+L (lint), Ctrl+F (search), F5 (refresh), Ctrl+Q (quit)
- **Menu accelerator hints** — keyboard shortcut labels shown in hamburger menu items
- **Workflow settings** — auto-lint before submit, auto-advance to next package after submit, cache TTL (hours)
- **Auto-advance** — automatically moves to next package after submit or add-to-queue
- **Error status icons** — packages with submission errors shown with ⚠️ icon in list

### Changed
- Sidebar width reduced from 250px to 220px with compact rows
- Editor panes are now horizontal (side-by-side) with equal sizing
- Menu restructured with sections and accelerator attributes
- Status bar replaces simple status label

## 0.4.0 (2026-02-16)

### Added
- **Statistics dialog** — fetch and display translation stats from ddtp.debian.org (Stats button in header + menu)
- **Queue dialog** — queue moved from right panel to a popup dialog (Show Queue button), cleaner 2-pane layout
- **Queue persistence** — queue is saved to disk and survives app restarts
- **Queue ETA** — estimated time shown before sending queue
- **Queue auto-cleanup** — successfully sent items are automatically removed; errors stay flagged
- **Lint integration** — lint current translation using l10n-lint (Lint button in header)
- **Event logging** — optional logging toggle in Preferences → Sending → Logging
- **Secure password storage** — SMTP password stored in system keyring when available (python-keyring)

### Changed
- Package data cache TTL increased from 1 hour to 24 hours
- Removed the third column (queue panel) from main window — now a popup dialog
- About dialog action correctly shows About window (was accidentally triggering DDTP fetch)

### Fixed
- "Om" (About) button no longer triggers "Failed to fetch DDTP data" error

## 0.3.2 (2026-02-15)

### Fixed
- User-friendly error messages when DDTP/mirror servers are unreachable instead of raw Python exceptions
- Improved error message in fallback path when both ddtp.debian.org and deb.debian.org mirror are down
- Verified About dialog (Om) works independently of network connectivity

## 0.3.1 (2026-02-15)

### Added
- Max packages display limit setting in Preferences (500 / 1000 / 5000 / All)
- Defaults to 500 for fast startup — Swedish has 72,000+ untranslated packages
- Stats label shows "500 of 72782 untranslated" when limited
- Export PO always exports all packages regardless of display limit

## 0.1.0 (2025-02-15)

- Initial release
- GTK4/Adwaita UI with side-by-side translation editor
- DDTP API client for fetching untranslated descriptions
- SMTP submission to pdesc@ddtp.debian.org
- Local caching (1h TTL)
- Configurable SMTP settings via Preferences window
- Search/filter for package list
- Support for all DDTP languages
