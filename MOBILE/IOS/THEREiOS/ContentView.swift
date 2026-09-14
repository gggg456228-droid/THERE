import SwiftUI

struct ContentView: View {
    @Environment(\.openURL) private var openURL

    private let serverURL = URL(string: "https://153-76-209-99.sslip.io/")!

    var body: some View {
        ZStack {
            Color(red: 0.035, green: 0.035, blue: 0.067)
                .ignoresSafeArea()

            VStack(spacing: 18) {
                Spacer()

                Text("THERE")
                    .font(.system(size: 42, weight: .bold))
                    .foregroundStyle(Color.yellow)

                Text("Серверная версия")
                    .font(.headline)
                    .foregroundStyle(Color.white.opacity(0.72))

                Button("Открыть THERE") {
                    openURL(serverURL)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)

                Text("Авторизация и сохранения работают на сервере. Сайт открывается только во внешнем браузере.")
                    .font(.footnote)
                    .foregroundStyle(Color.white.opacity(0.6))
                    .multilineTextAlignment(.center)

                Spacer()
            }
            .padding(24)
        }
    }
}
