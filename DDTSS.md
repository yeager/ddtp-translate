# DDTSS Integration — Submitting Translations

## Overview

ddtp-translate supports two methods for submitting translations to the
Debian Description Translation Project:

| Method | Status | How it works |
|--------|--------|--------------|
| **DDTSS (web)** | ✅ Recommended | HTTP POST to the DDTSS web interface |

The DDTSS (Debian Distributed Translation Server Satellite) is the official
web frontend at https://ddtp.debian.org/ddtss/index.cgi/

## Setup (DDTSS)

1. **Create an account** at https://ddtp.debian.org/ddtss/index.cgi/createlogin
   - Alias: alphanumeric, min 2 characters
   - Password: min 5 characters
   - Account must be activated by language team admin

2. **Configure in ddtp-translate**:
   - Open Preferences → DDTSS tab
   - Enter your alias and password
   - Click "Test Login" to verify
   - Set "Submit via" to "DDTSS (web)"

## How It Works

```
┌─────────────────┐     HTTP POST      ┌──────────────┐
│  ddtp-translate  │ ──────────────────►│    DDTSS     │
│  (GTK4 app)     │   login + submit   │  (web CGI)   │
└─────────────────┘                    └──────┬───────┘
                                              │
                                    review by other
                                     translators
                                              │
                                       ┌──────▼───────┐
                                       │     DDTP     │
                                       │  (database)  │
                                       └──────┬───────┘
                                              │
                                       ┌──────▼───────┐
                                       │ Debian repos │
                                       │ Translation- │
                                       │   sv.bz2     │
                                       └──────────────┘
```

1. User translates a package description in the GUI
2. App logs into DDTSS via HTTP (cookie-based auth, 70-day session)
3. App POSTs the translation to `/sv/translate/<package>`
4. Translation enters "for review" state on DDTSS
5. Other translators review and accept/refuse
6. After sufficient reviews, DDTSS sends it to the DDTP database
7. DDTP exports translations to Debian mirrors as `Translation-sv.bz2`

## DDTSS Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/<lang>/login` | POST | Authenticate (params: `alias`, `password`, `submit`) |
| `/<lang>/fetch` | GET | Fetch next package to translate (param: `package`) |
| `/<lang>/translate/<pkg>` | GET | Get translation form |
| `/<lang>/translate/<pkg>` | POST | Submit translation (params: `short`, `long`, `submit`) |
| `/<lang>/forreview/<pkg>` | GET | Get review form |
| `/<lang>/forreview/<pkg>` | POST | Accept/refuse (params: `accept`/`refuse`) |
| `/<lang>/` | GET | Main page with stats |
| `/createlogin` | GET/POST | Create new account |

Base URL: `https://ddtp.debian.org/ddtss/index.cgi`

## Error Codes

| Error Message | Cause | Resolution |
|--------------|-------|------------|
| `You must be logged in for this to work` | Session expired or not authenticated | Re-login |
| `Invalid username/password` | Wrong credentials | Check alias/password |
| `Account not active yet` | Account pending activation | Contact language team admin |
| `Translation not complete, still <trans>` | `<trans>` placeholder tags remain | Complete all translations |
| `Translation contains line longer than 80 characters` | Line exceeds 80 char limit | Rewrap text |
| `Translation <pkg> locked, sorry...` | Another user is editing | Wait and retry |
| `Package translation for <pkg> gone, sorry...` | Package removed from queue | Fetch a new package |
| `Couldn't fetch an untranslated description` | Package unavailable | Try another package |
| `Fetched description didn't contain package name` | Server data error | Report to debian-i18n |
| `Encoding error retrieving data for package <pkg>` | UTF-8 issue | Report to debian-i18n |
| `<pkg> was already in system` | Already queued (not an error) | Proceed to translate |
| HTTP 500 / connection error | Server down | Retry later |

## Swedish (sv) Notes

- Swedish requires login to edit (`requirelogin` is set)
- Current stats: 3 pending translation, 3 pending review, 340 sent
- 163 active translations out of 68,442 packages (0.2%)
- Contact: debian-i18n@lists.debian.org


## API Client

The `ddtss_client.py` module provides a Python API:

```python
from ddtp_translate.ddtss_client import DDTSSClient

client = DDTSSClient(lang="sv")
client.login("my_alias", "my_password")

# Fetch a package
pkg = client.fetch_package("apt")

# Submit translation
client.submit_translation("apt", "Pakethanterare", "Avancerat pakethanteringsverktyg...")

# Get stats
stats = client.get_stats()
print(f"Pending: {stats['pending_translation']}, Review: {stats['pending_review']}")
```

### Exception Hierarchy

```
DDTSSError (base)
├── DDTSSAuthError        — login/session issues
├── DDTSSLockedError      — package locked by another user
├── DDTSSNotFoundError    — package not available
└── DDTSSValidationError  — translation invalid
```
