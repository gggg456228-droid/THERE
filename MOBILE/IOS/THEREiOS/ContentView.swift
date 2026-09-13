import SwiftUI

struct ContentView: View {
    @AppStorage("there.server.url") private var serverURL = ""
    @Environment(\.openURL) private var openURL

    var body: some View {
        ZStack {
            Color(red: 0.035, green: 0.035, blue: 0.067)
                .ignoresSafeArea()

            VStack(spacing: 18) {
                Spacer()

                Text("THERE")
                    .font(.system(size: 42, weight: .bold))
                    .foregroundStyle(Color.yellow)

                TextField("https://your-there-server.example", text: $serverURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .padding(14)
                    .background(Color.white.opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .foregroundStyle(.white)

                Button("Открыть сайт") {
                    openSite()
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(normalizedURL == nil)

                Text("Сайт всегда открывается во внешнем браузере. Встроенного WebView здесь нет.")
                    .font(.footnote)
                    .foregroundStyle(Color.white.opacity(0.6))
                    .multilineTextAlignment(.center)

                Spacer()
            }
            .padding(24)
        }
    }

    private var normalizedURL: URL? {
        let value = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return nil }
        if let url = URL(string: value), url.scheme != nil {
            return url
        }
        return URL(string: "https://" + value)
    }

    private func openSite() {
        guard let url = normalizedURL else { return }
        openURL(url)
    }
}
