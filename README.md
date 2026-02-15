# DDTP Translate

A GTK4/Adwaita desktop application for translating Debian package descriptions via the [Debian Description Translation Project (DDTP)](https://ddtp.debian.org/).

![License](https://img.shields.io/badge/license-GPL--3.0-blue)

## Features

- Browse untranslated Debian package descriptions for any language
- Side-by-side editor: original English description + translation area
- Submit translations directly to DDTP via email (pdesc@ddtp.debian.org)
- Configurable SMTP settings
- Local caching with 1-hour TTL
- Search and filter packages
- Statistics view showing untranslated count per language
- Full i18n support

## Installation

### From .deb (Debian/Ubuntu)

```bash
echo "deb [signed-by=/usr/share/keyrings/yeager-archive-keyring.gpg] https://yeager.github.io/debian-repo stable main" | sudo tee /etc/apt/sources.list.d/yeager.list
curl -fsSL https://yeager.github.io/debian-repo/yeager-archive-keyring.gpg | sudo tee /usr/share/keyrings/yeager-archive-keyring.gpg > /dev/null
sudo apt update && sudo apt install ddtp-translate
```

### From .rpm (Fedora/RHEL)

```bash
sudo dnf config-manager --add-repo https://yeager.github.io/rpm-repo/yeager.repo
sudo dnf install ddtp-translate
```

### From source

```bash
pip install .
ddtp-translate
```

## Requirements

- Python 3.9+
- GTK 4
- libadwaita 1.x
- PyGObject

## Usage

1. Select your target language from the dropdown
2. Browse or search for untranslated packages in the sidebar
3. Click a package to see its English description
4. Write your translation in the right panel
5. Click **Submit Translation** to send via email to DDTP

Configure your SMTP settings in **Preferences** (hamburger menu → Preferences).

## Contributing

Translations are managed on [Transifex](https://www.transifex.com/danielnylander/ddtp-translate/).

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

## Author

Daniel Nylander <daniel@danielnylander.se>
