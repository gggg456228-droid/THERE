# THERE iOS

This iOS target intentionally contains no WebView, WKWebView or in-app browser. The user enters a THERE server URL and the site opens only after pressing "Открыть сайт", using the system browser.

Important iOS limitation: a localhost Flask server started inside this app cannot reliably keep running after Safari takes the foreground because iOS suspends normal background apps. Therefore this external-browser-only iOS target is a launcher for an externally reachable THERE server.

The source is verified by GitHub Actions against the iPhone Simulator. Installing on a physical iPhone requires Apple code signing in Xcode with the user's Apple ID or developer certificate.
