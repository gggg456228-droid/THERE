# Security and privacy

THERE is a local application. The bundled web server listens on `127.0.0.1` by default and application data is stored locally under `%LOCALAPPDATA%\THERE\data` on Windows.

This public repository must not contain personal profiles, character databases copied from a user's machine, session keys, API tokens, passwords, private keys, machine-specific user paths, personal contact details, or private signing keys.

The Windows updater downloads only from the public `gggg456228-droid/THERE` repository and verifies the SHA-256 published in `update/latest.json` before applying an update bundle.

The Android APK from the older 2.6.4 archive is intentionally not published here because its signing certificate contained an identifying developer handle. Android should be rebuilt with a clean project signing identity before a public APK is added.

If a secret or personal value is found in the public tree, remove it from the current tree and rotate/revoke the secret if applicable. Treat Git history as public once pushed.
