# THERE

THERE is a local character and campaign application. The Windows release is a single self-contained `THERE.exe`.

## Windows

Download `dist/THERE_WINDOWS_X64.zip`, extract it, and run `THERE.exe`.

The executable already contains its Python runtime and the required Flask, Waitress, ReportLab and application modules. A separate Python installation and `pip install` commands are not required.

THERE listens only on `127.0.0.1` and chooses a new local port on every launch. The browser opens automatically. Keep the console window open while using THERE; closing it stops the local server.

User data is stored separately in `%LOCALAPPDATA%\THERE\data`, so application updates do not overwrite characters or local backups.

## Automatic updates

On startup THERE reads `update/latest.json`. If a newer release is available, it downloads `dist/THERE_WINDOWS_X64.zip`, verifies its SHA-256, replaces `THERE.exe` after the running process exits, and starts the new version automatically.

## Build

The public workflow reconstructs the application build payload under `WINDOWS_X64/source_payload`, runs a Flask health smoke test, compiles a one-file Windows executable with Nuitka, runs a self-test on the compiled EXE, creates the update ZIP, calculates hashes, and publishes the generated files back to `main`.

Private Android signing keys, historical handoff archives, local user data and machine-specific secrets are not part of this repository.
