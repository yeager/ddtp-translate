"""Application settings for ddtp-translate."""

import json
import os
from pathlib import Path


def _config_dir():
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    d = Path(xdg) / "ddtp-translate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _settings_path():
    return _config_dir() / "settings.json"


_KEYRING_SERVICE = "ddtp-translate"
_KEYRING_KEY = "ddtss_password"


def _get_keyring():
    """Try to import keyring, return None if unavailable."""
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def load_settings():
    """Load application settings from config file."""
    path = _settings_path()
    defaults = {
        "ddtss_alias": "",
        "ddtss_password": "",
        "default_language": "sv",
        "send_delay": 30,
        "max_packages": 500,
        "enable_logging": False,
    }
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            defaults.update(saved)
        except (json.JSONDecodeError, OSError):
            pass

    # Try to load password from keyring
    kr = _get_keyring()
    if kr:
        try:
            pw = kr.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
            if pw is not None:
                defaults["ddtss_password"] = pw
        except Exception:
            pass

    # Remove legacy SMTP settings if present
    for key in list(defaults.keys()):
        if key.startswith("smtp_") or key in ("from_email", "from_name", "submit_method"):
            del defaults[key]

    return defaults


def save_settings(settings):
    """Save application settings to config file."""
    path = _settings_path()

    # Try to store password in keyring
    password = settings.get("ddtss_password", "")
    kr = _get_keyring()
    stored_in_keyring = False
    if kr and password:
        try:
            kr.set_password(_KEYRING_SERVICE, _KEYRING_KEY, password)
            stored_in_keyring = True
        except Exception:
            pass

    # Save to file — omit password if stored in keyring
    to_save = dict(settings)
    if stored_in_keyring:
        to_save.pop("ddtss_password", None)

    # Remove any legacy SMTP keys
    for key in list(to_save.keys()):
        if key.startswith("smtp_") or key in ("from_email", "from_name", "submit_method"):
            del to_save[key]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_save, f, indent=2, ensure_ascii=False)
