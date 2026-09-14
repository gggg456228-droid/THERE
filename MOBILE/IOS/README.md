# THERE iOS

This iOS target intentionally contains no WebView, WKWebView or in-app browser. The app opens the production THERE server only after pressing "Открыть THERE", using the system browser.

Production server:

`https://153-76-209-99.sslip.io/`

Authentication, account data and saved THERE state are handled by the VPS server. The iOS app does not contain VPS credentials or private server secrets.

The source is verified by GitHub Actions against the iPhone Simulator. Installing on a physical iPhone requires Apple code signing in Xcode with the user's Apple ID or developer certificate.
