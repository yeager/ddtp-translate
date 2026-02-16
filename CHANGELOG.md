# Changelog

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
