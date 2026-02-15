"""DDTP HTTP API client — fetch untranslated Debian package descriptions."""

import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

DDTP_BASE = "https://ddtp.debian.org/ddt.cgi"
# Fallback: fetch from Debian mirror i18n Translation files
MIRROR_BASE = "https://deb.debian.org/debian"
DISTS = ["sid", "trixie", "bookworm"]
CACHE_TTL = 3600  # 1 hour

# All DDTP-supported language codes
DDTP_LANGUAGES = [
    ("ar", "Arabic"),
    ("bg", "Bulgarian"),
    ("ca", "Catalan"),
    ("cs", "Czech"),
    ("da", "Danish"),
    ("de", "German"),
    ("el", "Greek"),
    ("eo", "Esperanto"),
    ("es", "Spanish"),
    ("eu", "Basque"),
    ("fi", "Finnish"),
    ("fr", "French"),
    ("gl", "Galician"),
    ("hu", "Hungarian"),
    ("id", "Indonesian"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("km", "Khmer"),
    ("ko", "Korean"),
    ("lt", "Lithuanian"),
    ("ml", "Malayalam"),
    ("nb", "Norwegian Bokmål"),
    ("nl", "Dutch"),
    ("pl", "Polish"),
    ("pt", "Portuguese"),
    ("pt_BR", "Brazilian Portuguese"),
    ("ro", "Romanian"),
    ("ru", "Russian"),
    ("sk", "Slovak"),
    ("sl", "Slovenian"),
    ("sr", "Serbian"),
    ("sv", "Swedish"),
    ("th", "Thai"),
    ("tr", "Turkish"),
    ("uk", "Ukrainian"),
    ("vi", "Vietnamese"),
    ("zh_CN", "Chinese (Simplified)"),
    ("zh_TW", "Chinese (Traditional)"),
]


def _cache_dir():
    xdg = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    d = Path(xdg) / "ddtp-translate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(lang):
    return _cache_dir() / f"untranslated_{lang}.json"


def _is_cache_valid(path):
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < CACHE_TTL


def compute_description_hash(description):
    """Compute MD5 hash of the description (used in DDTP email subject)."""
    return hashlib.md5(description.encode("utf-8")).hexdigest()


def parse_ddtp_response(text):
    """Parse DDTP ddt.cgi response into list of package dicts.

    Response format (one per package, separated by blank lines):
        Package: <name>
        Description-md5: <hash>
        Description-en: <short description>
         <long description line 1>
         <long description line 2>
         .
    """
    packages = []
    current = None

    for line in text.splitlines():
        if line.startswith("Package: "):
            if current:
                packages.append(current)
            current = {
                "package": line[9:].strip(),
                "md5": "",
                "short": "",
                "long": "",
            }
        elif line.startswith("Description-md5: ") and current:
            current["md5"] = line[17:].strip()
        elif line.startswith("Description-en: ") and current:
            current["short"] = line[16:].strip()
        elif current and (line.startswith(" ") or line.startswith("\t")):
            stripped = line.strip()
            if stripped == ".":
                current["long"] += "\n"
            else:
                if current["long"]:
                    current["long"] += "\n"
                current["long"] += stripped

    if current:
        packages.append(current)

    return packages


def fetch_untranslated(lang, force_refresh=False):
    """Fetch untranslated descriptions for a language. Returns list of dicts."""
    cache = _cache_path(lang)

    if not force_refresh and _is_cache_valid(cache):
        with open(cache, "r", encoding="utf-8") as f:
            return json.load(f)

    url = f"{DDTP_BASE}?lcode={lang}&getuntranslated=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ddtp-translate/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        # If we have stale cache, use it
        if cache.exists():
            with open(cache, "r", encoding="utf-8") as f:
                return json.load(f)
        raise RuntimeError(f"Failed to fetch DDTP data: {exc}") from exc

    packages = parse_ddtp_response(text)

    with open(cache, "w", encoding="utf-8") as f:
        json.dump(packages, f, ensure_ascii=False, indent=2)

    return packages


def _fetch_translation_file(dist, lang):
    """Download and decompress Translation-XX.bz2 from Debian mirror."""
    import bz2
    url = f"{MIRROR_BASE}/dists/{dist}/main/i18n/Translation-{lang}.bz2"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ddtp-translate/0.1"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            compressed = resp.read()
        return bz2.decompress(compressed).decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_translation_file(text):
    """Parse Debian Translation file into dict of md5 -> {package, short, long}."""
    entries = {}
    current = {}
    in_desc = False

    for line in text.splitlines():
        if line.startswith("Package: "):
            if current.get("md5"):
                entries[current["md5"]] = current
            current = {"package": line[9:].strip(), "md5": "", "short": "", "long": ""}
            in_desc = False
        elif line.startswith("Description-md5: "):
            current["md5"] = line[17:].strip()
        elif line.startswith("Description-"):
            current["short"] = line.split(": ", 1)[1] if ": " in line else ""
            in_desc = True
        elif in_desc and (line.startswith(" ") or line.startswith("\t")):
            stripped = line.strip()
            if stripped == ".":
                current["long"] += "\n"
            else:
                if current["long"]:
                    current["long"] += "\n"
                current["long"] += stripped
        else:
            in_desc = False

    if current.get("md5"):
        entries[current["md5"]] = current
    return entries


def fetch_untranslated_from_mirror(lang, dist="sid"):
    """Fetch untranslated descriptions by comparing en vs lang Translation files."""
    cache = _cache_dir() / f"mirror_untranslated_{lang}_{dist}.json"

    if _is_cache_valid(cache):
        with open(cache, "r", encoding="utf-8") as f:
            return json.load(f)

    en_text = _fetch_translation_file(dist, "en")
    if not en_text:
        raise RuntimeError(f"Failed to download Translation-en for {dist}")

    lang_text = _fetch_translation_file(dist, lang)

    en_entries = _parse_translation_file(en_text)
    lang_entries = _parse_translation_file(lang_text) if lang_text else {}

    # Untranslated = in en but not in lang
    untranslated = []
    for md5, entry in en_entries.items():
        if md5 not in lang_entries:
            untranslated.append(entry)

    # Sort by package name
    untranslated.sort(key=lambda x: x.get("package", ""))

    with open(cache, "w", encoding="utf-8") as f:
        json.dump(untranslated, f, ensure_ascii=False, indent=2)

    return untranslated


def fetch_untranslated(lang, force_refresh=False):
    """Fetch untranslated descriptions for a language. Returns list of dicts.
    
    Tries ddtp.debian.org first, falls back to Debian mirror comparison.
    """
    cache = _cache_path(lang)

    if not force_refresh and _is_cache_valid(cache):
        with open(cache, "r", encoding="utf-8") as f:
            return json.load(f)

    # Try ddtp.debian.org first
    url = f"{DDTP_BASE}?lcode={lang}&getuntranslated=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ddtp-translate/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        packages = parse_ddtp_response(text)
        if packages:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(packages, f, ensure_ascii=False, indent=2)
            return packages
    except Exception:
        pass

    # Fallback: Debian mirror comparison
    try:
        packages = fetch_untranslated_from_mirror(lang, "sid")
        if packages:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(packages, f, ensure_ascii=False, indent=2)
            return packages
    except Exception:
        pass

    # Last resort: stale cache
    if cache.exists():
        with open(cache, "r", encoding="utf-8") as f:
            return json.load(f)

    raise RuntimeError(
        "Could not fetch DDTP data. Both ddtp.debian.org and "
        "Debian mirror are unreachable."
    )


def get_statistics(lang):
    """Return (untranslated_count,) for a language."""
    pkgs = fetch_untranslated(lang)
    return len(pkgs)
