# Changelog

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
