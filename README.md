# THERE

THERE is a local character/campaign management application. The web UI is served only on `127.0.0.1:33851` by default.

## Windows auto-update

`THERE.exe` checks the GitHub update manifest on startup. The launcher reads `update/latest.json`; when a newer version is available it downloads the current Windows bundle, verifies its SHA-256, stages the update, replaces the application files after the running launcher exits, and starts the new version automatically.

The Python entry point also performs the same update check as a fallback for older launchers. If GitHub is temporarily unavailable, update checking fails open and THERE starts the installed version.

For updater installations that do not contain GitHub credentials, the repository or the update files referenced by `update/latest.json` must be publicly readable. Do not embed a personal GitHub token in THERE.

User data is stored outside the application directory in `%LOCALAPPDATA%\THERE\data`. The updater replaces application files only and does not overwrite characters, local campaign data, or backups.

## Runtime packaging

The complete application source used by the Windows release is stored in `WINDOWS_X64/runtime/app/full_app.zip`. The release workflow extracts it and vendors the Python dependencies from `requirements.txt` into `runtime/packages`, so a generated update is not missing Flask, Waitress, templates, or other runtime files.

## Updating a release

Bump `WINDOWS_X64/VERSION.txt` and push source changes. GitHub Actions rebuilds `WINDOWS_X64/THERE.exe`, creates the portable Windows bundle, refreshes `update/latest.json`, and commits generated artifacts with `[skip ci]` to avoid a workflow loop.

No Git installation or private GitHub authentication is required on the player's PC when the update endpoint is public.
