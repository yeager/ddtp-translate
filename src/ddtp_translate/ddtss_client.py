"""DDTSS HTTP client — submit translations via the DDTSS web interface.

The DDTSS (Debian Distributed Translation Server Satellite) is the official
web frontend for contributing Debian package description translations.

Base URL: https://ddtp.debian.org/ddtss/index.cgi

Authentication:
    Cookie-based. Login sets a cookie 'id' valid for 70 days.

Endpoints used:
    POST /login              — authenticate (params: alias, password, submit=Submit)
    GET  /<lang>/fetch       — fetch a package to translate (param: package)
    GET  /<lang>/translate/<pkg> — get translation form for a package
    POST /<lang>/translate/<pkg> — submit translation (params: short, long, submit=1)
    GET  /<lang>/forreview/<pkg> — get review form
    POST /<lang>/forreview/<pkg> — accept/refuse review (params: accept/refuse)
    GET  /<lang>/             — main page with stats

Error codes / responses:
    "You must be logged in for this to work"
        → Login required. Re-authenticate and retry.
    "Translation not complete, still <trans>"
        → The translation still contains <trans> placeholder tags.
    "Translation contains line longer than 80 characters"
        → Line length limit exceeded. Rewrap the text.
    "Translation <pkg> locked, sorry..."
        → Another user is currently editing this package.
    "Package translation for <pkg> gone, sorry..."
        → The package was removed from the queue between fetch and submit.
    "Couldn't fetch an untranslated description: <lang>, <email>, <pkg>"
        → Package not available for translation (already translated or not found).
    "Fetched description didn't contain package name"
        → Server-side data error.
    "Encoding error retrieving data for package <pkg>"
        → UTF-8 encoding issue in the package data.
    "Invalid username/password"
        → Wrong credentials.
    "Account not active yet"
        → Account created but not yet activated by a language team admin.
    "<pkg> was already in system"
        → Package already queued for translation (not an error, proceeds to translate).
    HTTP 500 / connection error
        → DDTSS server is down or unreachable.

Flow:
    1. Login with alias + password → receive session cookie
    2. Fetch a package (or use one already in queue)
    3. GET translate page → parse original description
    4. POST translation (short + long description)
    5. Translation enters 'forreview' state
    6. After sufficient reviews, DDTSS sends it to DDTP automatically
"""

import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

DDTSS_BASE = "https://ddtp.debian.org/ddtss/index.cgi"
USER_AGENT = "ddtp-translate/0.6.1 (GTK4; +https://github.com/yeager/ddtp-translate)"

# XDG config for cookie persistence
_XDG = Path.home() / ".config" / "ddtp-translate"


class DDTSSError(Exception):
    """Base error for DDTSS operations."""
    pass


class DDTSSAuthError(DDTSSError):
    """Authentication failed."""
    pass


class DDTSSLockedError(DDTSSError):
    """Package is locked by another user."""
    pass


class DDTSSNotFoundError(DDTSSError):
    """Package not found or not available."""
    pass


class DDTSSValidationError(DDTSSError):
    """Translation validation failed (e.g., <trans> tags, line length)."""
    pass


class _FormParser(HTMLParser):
    """Extract form fields and error messages from DDTSS HTML."""

    def __init__(self):
        super().__init__()
        self.fields = {}  # name → value
        self.textareas = {}  # name → content
        self._in_textarea = None
        self._textarea_buf = []
        self._in_h1 = False
        self.title = ""
        self.error_message = ""
        self._in_body = False

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "input" and "name" in attrs_d:
            self.fields[attrs_d["name"]] = attrs_d.get("value", "")
        if tag == "textarea" and "name" in attrs_d:
            self._in_textarea = attrs_d["name"]
            self._textarea_buf = []
        if tag == "h1":
            self._in_h1 = True

    def handle_endtag(self, tag):
        if tag == "textarea" and self._in_textarea:
            self.textareas[self._in_textarea] = "".join(self._textarea_buf)
            self._in_textarea = None
        if tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._in_textarea:
            self._textarea_buf.append(data)
        if self._in_h1:
            self.title += data


class DDTSSClient:
    """HTTP client for the DDTSS web interface."""

    def __init__(self, lang="sv"):
        self.lang = lang
        self._cookie_jar = http.cookiejar.MozillaCookieJar()
        self._cookie_file = _XDG / "ddtss_cookies.txt"
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self._load_cookies()

    def _load_cookies(self):
        """Load saved cookies if available."""
        try:
            if self._cookie_file.exists():
                self._cookie_jar.load(str(self._cookie_file), ignore_discard=True)
        except Exception:
            pass

    def _save_cookies(self):
        """Persist cookies to disk."""
        self._cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self._cookie_jar.save(str(self._cookie_file), ignore_discard=True)

    def _request(self, url, data=None, method="GET", multipart=False):
        """Make an HTTP request and return (status_code, body_text).

        Raises DDTSSError on connection failures.
        """
        headers = {"User-Agent": USER_AGENT}
        if data is not None:
            if multipart:
                # DDTSS forms use multipart/form-data encoding
                boundary = "----DDTPTranslateBoundary"
                parts = []
                for key, value in data.items():
                    parts.append(
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                        f"{value}\r\n"
                    )
                parts.append(f"--{boundary}--\r\n")
                body = "".join(parts).encode("utf-8")
                headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            else:
                encoded = urllib.parse.urlencode(data).encode("utf-8")
                req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
        else:
            req = urllib.request.Request(url, headers=headers, method=method)

        try:
            resp = self._opener.open(req, timeout=30)
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            return e.code, body
        except urllib.error.URLError as e:
            raise DDTSSError(f"Connection error: {e.reason}") from e

    def _check_error(self, body):
        """Check response body for known DDTSS error messages."""
        # Parse for error in <h1> or plain text
        error_patterns = {
            "You must be logged in": DDTSSAuthError,
            "Invalid username/password": DDTSSAuthError,
            "Account not active yet": DDTSSAuthError,
            "locked, sorry": DDTSSLockedError,
            "gone, sorry": DDTSSNotFoundError,
            "Couldn't fetch": DDTSSNotFoundError,
            "didn't contain package name": DDTSSNotFoundError,
            "Encoding error": DDTSSError,
            "not complete, still <trans>": DDTSSValidationError,
            "line longer than 80 characters": DDTSSValidationError,
        }
        for pattern, exc_class in error_patterns.items():
            if pattern in body:
                # Extract the full error message from <h1>
                m = re.search(r"<h1>(.*?)</h1>", body, re.DOTALL)
                msg = m.group(1).strip() if m else pattern
                # Strip HTML tags from message
                msg = re.sub(r"<[^>]+>", "", msg).strip()
                raise exc_class(msg)

    def login(self, alias, password):
        """Authenticate with the DDTSS.

        Args:
            alias: DDTSS username (alphanumeric, min 2 chars)
            password: Password (min 5 chars)

        Raises:
            DDTSSAuthError: Invalid credentials or inactive account.
            DDTSSError: Connection or server error.

        Returns:
            True on success.
        """
        url = f"{DDTSS_BASE}/login"
        data = {
            "alias": alias,
            "password": password,
            "submit": "Submit",
        }
        status, body = self._request(url, data=data, multipart=True)
        self._check_error(body)

        # Successful login redirects to main page or shows logged-in status
        if any(phrase in body for phrase in (
            "Logged in as",
            "logged in",
            "Pending translation",
            "Pending review",
        )):
            self._save_cookies()
            return True

        # Check if we got a session cookie (redirect may have happened)
        for cookie in self._cookie_jar:
            if cookie.name == "id" and "ddtp.debian.org" in (cookie.domain or ""):
                self._save_cookies()
                return True

        raise DDTSSAuthError("Login failed (unexpected response)")

    def is_logged_in(self):
        """Check if we have a valid session cookie."""
        for cookie in self._cookie_jar:
            if cookie.name == "id" and "ddtp.debian.org" in (cookie.domain or ""):
                if not cookie.is_expired():
                    return True
        return False

    def fetch_package(self, package=None):
        """Fetch a package for translation.

        Args:
            package: Specific package name, or None for next available.

        Returns:
            dict with keys: package, short_orig, long_orig, short_trans, long_trans

        Raises:
            DDTSSAuthError: Not logged in.
            DDTSSNotFoundError: Package not available.
            DDTSSLockedError: Package locked by another user.
        """
        url = f"{DDTSS_BASE}/{self.lang}/fetch"
        if package:
            url += f"?package={urllib.parse.quote(package)}"

        status, body = self._request(url)
        self._check_error(body)

        # The fetch page redirects to /translate/<pkg> or /forreview/<pkg>
        # Check for redirect URL in meta refresh or body
        m = re.search(r'url=([^"]+/(?:translate|forreview)/([\w.+-]+))', body)
        if m:
            redirect_url = m.group(1)
            pkg_name = m.group(2)
            if not redirect_url.startswith("http"):
                redirect_url = f"{DDTSS_BASE}/{self.lang}/{redirect_url.split('/' + self.lang + '/')[-1]}"

            # Follow the redirect to get the actual form
            status, body = self._request(redirect_url)
            self._check_error(body)
            return self._parse_translate_page(body, pkg_name)

        # Check if we got a translate page directly
        parser = _FormParser()
        parser.feed(body)
        if parser.textareas:
            pkg_match = re.search(r"translate/([\w.+-]+)", url)
            pkg_name = pkg_match.group(1) if pkg_match else "unknown"
            return self._parse_translate_page(body, pkg_name)

        raise DDTSSNotFoundError("No package available for translation")

    def get_translate_page(self, package):
        """Get the translation form for a specific package.

        Returns:
            dict with keys: package, short_orig, long_orig, short_trans, long_trans
        """
        url = f"{DDTSS_BASE}/{self.lang}/translate/{urllib.parse.quote(package)}"
        status, body = self._request(url)
        self._check_error(body)
        return self._parse_translate_page(body, package)

    def _parse_translate_page(self, body, package):
        """Parse the translate/review HTML page into structured data."""
        parser = _FormParser()
        parser.feed(body)

        result = {
            "package": package,
            "short_orig": "",
            "long_orig": "",
            "short_trans": parser.textareas.get("short", parser.fields.get("short", "")),
            "long_trans": parser.textareas.get("long", ""),
        }

        # Extract original description from the page
        # It's usually in a <pre> or rendered text before the form
        orig_short_m = re.search(
            r"Description:\s*(.*?)(?:\n|<br)", body
        )
        if orig_short_m:
            result["short_orig"] = orig_short_m.group(1).strip()

        # Extract long original description
        orig_long_m = re.search(
            r'class=["\']?untranslated["\']?[^>]*>(.*?)</(?:pre|div|td)',
            body, re.DOTALL
        )
        if orig_long_m:
            text = re.sub(r"<[^>]+>", "", orig_long_m.group(1))
            result["long_orig"] = text.strip()

        return result

    def submit_translation(self, package, short, long, comment=""):
        """Submit a translated description.

        Automatically fetches the package first if needed (DDTSS requires
        a fetch before the translate form is available).

        Args:
            package: Package name.
            short: Translated short description (single line, max 80 chars).
            long: Translated long description (each line max 80 chars).
            comment: Optional comment for reviewers.

        Returns:
            True on success.

        Raises:
            DDTSSValidationError: Translation invalid (<trans> tags or line length).
            DDTSSLockedError: Package locked by another user.
            DDTSSAuthError: Not logged in.
        """
        pkg_quoted = urllib.parse.quote(package)

        # Step 1: Fetch the package to ensure translate form is available
        fetch_url = f"{DDTSS_BASE}/{self.lang}/fetch?package={pkg_quoted}"
        f_status, f_body = self._request(fetch_url)
        self._check_error(f_body)

        # Step 2: GET the translate page to confirm it's ready
        translate_url = f"{DDTSS_BASE}/{self.lang}/translate/{pkg_quoted}"
        g_status, g_body = self._request(translate_url)
        self._check_error(g_body)

        # Verify we got the actual translate form, not a "Fetching..." page
        if "Fetching package" in g_body:
            raise DDTSSError(f"Package {package} not available for translation")

        # Step 3: POST the translation
        data = {
            "short": short,
            "long": long,
            "comment": comment,
            "submit": "Submit",
            "_charset_": "UTF-8",
        }

        status, body = self._request(translate_url, data=data, multipart=True)
        self._check_error(body)

        # Check for success confirmation
        if "submitted" in body.lower():
            return True

        # If no error was raised and we got HTTP 200, treat as success
        if status in (200, 301, 302):
            return True

        raise DDTSSError(f"Unexpected response after submit (HTTP {status})")

    def get_pending_reviews(self):
        """Get list of packages pending review.

        Returns:
            list of dicts with keys: package, timestamp, note, owner
        """
        url = f"{DDTSS_BASE}/{self.lang}/"
        status, body = self._request(url)

        reviews = []
        # Parse "Pending review" section
        review_section = re.search(
            r'Pending review.*?<ol>(.*?)</ol>', body, re.DOTALL
        )
        if review_section:
            for m in re.finditer(
                r'forreview/([\w.+-]+)\?(\d+)">([\w.+-]+)</a>\s*\(([^)]*)\)',
                review_section.group(1)
            ):
                reviews.append({
                    "package": m.group(1),
                    "timestamp": m.group(2),
                    "note": m.group(4),
                })

        # Also parse "Reviewed by you" section
        reviewed_section = re.search(
            r'Reviewed by you.*?<ol>(.*?)</ol>', body, re.DOTALL
        )
        if reviewed_section:
            for m in re.finditer(
                r'forreview/([\w.+-]+)\?(\d+)">([\w.+-]+)</a>\s*\(([^)]*)\)',
                reviewed_section.group(1)
            ):
                reviews.append({
                    "package": m.group(1),
                    "timestamp": m.group(2),
                    "note": m.group(4),
                    "reviewed_by_you": True,
                })

        return reviews

    def get_review_page(self, package):
        """Get the review form for a specific package.

        Returns:
            dict with keys: package, short_orig, long_orig, short_trans, long_trans,
                            owner, reviewers, log, diff_html, comment
        """
        url = f"{DDTSS_BASE}/{self.lang}/forreview/{urllib.parse.quote(package)}"
        status, body = self._request(url)
        self._check_error(body)

        parser = _FormParser()
        parser.feed(body)

        result = {
            "package": package,
            "short_orig": "",
            "long_orig": "",
            "short_trans": parser.fields.get("short", ""),
            "long_trans": parser.textareas.get("long", ""),
            "comment": parser.textareas.get("comment", ""),
            "owner": "",
            "log": "",
        }

        # Extract original short description
        orig_short_m = re.search(r'Untranslated:\s*<tt>(.*?)</tt>', body)
        if orig_short_m:
            from html import unescape
            result["short_orig"] = unescape(orig_short_m.group(1).strip())

        # Extract original long description from <pre> in untranslated
        orig_long_m = re.search(r'Untranslated:.*?<pre>(.*?)</pre>', body, re.DOTALL)
        if orig_long_m:
            result["long_orig"] = orig_long_m.group(1).strip()

        # Extract owner
        owner_m = re.search(r'the owner is:\s*<b>(.*?)</b>', body)
        if owner_m:
            result["owner"] = owner_m.group(1)

        # Extract log
        log_m = re.search(r'Log:\s*<pre>(.*?)</pre>', body, re.DOTALL)
        if log_m:
            result["log"] = log_m.group(1).strip()

        return result

    def submit_review(self, package, action="accept", short="", long="", comment=""):
        """Submit a review for a translation.

        Args:
            package: Package name.
            action: "accept" (accept as is), "changes" (accept with changes),
                    or "comment" (change comment only).
            short: Updated short description (only for action="changes").
            long: Updated long description (only for action="changes").
            comment: Optional comment.

        Returns:
            True on success.
        """
        url = f"{DDTSS_BASE}/{self.lang}/forreview/{urllib.parse.quote(package)}"
        data = {"_charset_": "UTF-8", "comment": comment}

        if action == "accept":
            data["accept"] = "Accept as is"
        elif action == "changes":
            data["submit"] = "Accept with changes"
            data["short"] = short
            data["long"] = long
        elif action == "comment":
            data["nothing"] = "Change comment only"
        else:
            raise ValueError(f"Unknown review action: {action}")

        status, body = self._request(url, data=data, multipart=True)
        self._check_error(body)
        return True

    def abandon(self, package):
        """Abandon a translation in progress.

        Args:
            package: Package name.

        Returns:
            True on success.
        """
        url = f"{DDTSS_BASE}/{self.lang}/translate/{urllib.parse.quote(package)}"
        data = {"abandon": "Abandon", "_charset_": "UTF-8"}
        status, body = self._request(url, data=data, multipart=True)
        self._check_error(body)
        return True

    def get_stats(self):
        """Get translation statistics for the current language.

        Returns:
            dict with keys: pending_translation, pending_review, sent
        """
        url = f"{DDTSS_BASE}/{self.lang}/"
        status, body = self._request(url)

        stats = {
            "pending_translation": 0,
            "pending_review": 0,
            "sent": 0,
        }

        # Parse stats table from main page
        m = re.search(
            r"Pending translation.*?(\d+).*?Pending review.*?(\d+).*?Sent.*?(\d+)",
            body, re.DOTALL
        )
        if m:
            stats["pending_translation"] = int(m.group(1))
            stats["pending_review"] = int(m.group(2))
            stats["sent"] = int(m.group(3))

        return stats
