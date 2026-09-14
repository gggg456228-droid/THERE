# THERE

## Download

Latest Windows EXE:
https://raw.githubusercontent.com/gggg456228-droid/THERE/main/WINDOWS_X64/THERE.exe

Latest Windows ZIP:
https://raw.githubusercontent.com/gggg456228-droid/THERE/main/dist/THERE_WINDOWS_X64.zip

Latest Android ARM64 APK:
https://raw.githubusercontent.com/gggg456228-droid/THERE/main/dist/THERE_ANDROID_ARM64.apk

Current release: `2.6.9`.

THERE is a local character and campaign application. The Windows release is a single self-contained `THERE.exe`.

## Windows

The executable already contains its Python runtime and required application modules. A separate Python installation is not required.

THERE listens only on `127.0.0.1` and chooses a local port on launch. User data is stored separately in `%LOCALAPPDATA%\THERE\data`, so application updates do not overwrite characters or local backups.

## Automatic updates

On startup THERE reads `update/latest.json`. If a newer Windows release is available, it downloads the update ZIP, verifies SHA-256, replaces `THERE.exe` after the running process exits, and starts the new version automatically.

## Mobile

The Android build is ARM64. The public build is debug-signed by CI. The iOS project source is included under `MOBILE/IOS`, but a signed IPA requires Apple signing and is not published here.
