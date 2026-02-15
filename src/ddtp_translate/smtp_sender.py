"""SMTP sender for submitting translations to DDTP."""

import json
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

DDTP_EMAIL = "pdesc@ddtp.debian.org"
DEFAULT_SMTP_HOST = ""
DEFAULT_SMTP_PORT = 587


def _config_dir():
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    d = Path(xdg) / "ddtp-translate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _settings_path():
    return _config_dir() / "settings.json"


def load_settings():
    """Load SMTP settings from config file."""
    path = _settings_path()
    defaults = {
        "smtp_host": DEFAULT_SMTP_HOST,
        "smtp_port": DEFAULT_SMTP_PORT,
        "smtp_user": "",
        "smtp_password": "",
        "smtp_use_tls": True,
        "from_email": "",
        "from_name": "",
        "default_language": "sv",
    }
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            defaults.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def save_settings(settings):
    """Save SMTP settings to config file."""
    path = _settings_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


def build_translation_email(package_name, md5_hash, lang, translated_short, translated_long, settings):
    """Build an email in DDTP format.

    Subject format: [DDTP] <package> <md5>
    Body format:
        Package: <name>
        Description-md5: <hash>
        Description-<lang>: <short>
         <long>
         .
    """
    body_lines = [
        f"Package: {package_name}",
        f"Description-md5: {md5_hash}",
        f"Description-{lang}: {translated_short}",
    ]

    for line in translated_long.splitlines():
        if not line.strip():
            body_lines.append(" .")
        else:
            body_lines.append(f" {line}")

    body = "\n".join(body_lines) + "\n"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[DDTP] {package_name} {md5_hash}"
    msg["To"] = DDTP_EMAIL
    msg["From"] = settings.get("from_email", "")
    if settings.get("from_name"):
        msg["From"] = f"{settings['from_name']} <{settings['from_email']}>"

    return msg


def send_translation(package_name, md5_hash, lang, translated_short, translated_long, settings=None):
    """Send a translation email to DDTP."""
    if settings is None:
        settings = load_settings()

    msg = build_translation_email(
        package_name, md5_hash, lang, translated_short, translated_long, settings
    )

    host = settings.get("smtp_host", DEFAULT_SMTP_HOST)
    port = int(settings.get("smtp_port", DEFAULT_SMTP_PORT))
    user = settings.get("smtp_user", "")
    password = settings.get("smtp_password", "")
    use_tls = settings.get("smtp_use_tls", True)

    if not host:
        raise RuntimeError(
            "SMTP server not configured. Go to Preferences to set up your mail server."
        )

    if use_tls:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        try:
            server.starttls()
        except smtplib.SMTPNotSupportedError:
            pass

    try:
        if user and password:
            server.login(user, password)
        server.send_message(msg)
    finally:
        server.quit()
