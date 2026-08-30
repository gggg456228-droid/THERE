# THERE

THERE is a local character/campaign management application. The web UI is served only on `127.0.0.1:33851` by default.

## Windows auto-update

`THERE.exe` checks this public GitHub repository on startup. The launcher reads `update/latest.json`; when a newer version is available it downloads the current Windows bundle, verifies its SHA-256, stages the update, replaces the application files after the running launcher exits, and starts the new version automatically.

If GitHub is temporarily unavailable, update checking fails open and THERE starts the installed version.

User data is stored outside the application directory in `%LOCALAPPDATA%\THERE\data`. The updater replaces application files only and does not overwrite characters, local campaign data, or backups.

## Public source

The public tree is intentionally cleaned of local user data, session keys, private signing keys, machine-specific user paths, personal contact details, and old Android signing metadata.

The old Android APK is not published here because the supplied APK was signed with an identifying developer certificate. A future Android release should be rebuilt and signed with a clean project identity before publication.

## Updating a release

Bump `WINDOWS_X64/VERSION.txt` and push source changes. GitHub Actions rebuilds `WINDOWS_X64/THERE.exe`, creates the portable Windows bundle, refreshes `update/latest.json`, and commits generated artifacts with `[skip ci]` to avoid a workflow loop.

No Git installation or private GitHub authentication is required on the player's PC.
