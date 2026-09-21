# Contributing translations

DDTP Translate accepts translations through GitHub pull requests. This keeps
every proposed catalogue reviewable alongside the source strings and does not
require a third-party translation account.

1. Create or update `po/<locale>.po` from `po/ddtp-translate.pot` with a
   gettext editor such as Poedit or Lokalize.
2. Keep the PO header accurate, including language, translator name and year.
3. Validate the catalogue locally:

   ```bash
   msgfmt --check po/<locale>.po
   ```

4. Open a pull request describing the language and the scope of the update.

Maintainers compile accepted catalogues into the release package. Do not commit
generated `.mo` files.
