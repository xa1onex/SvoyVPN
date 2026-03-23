import SwiftUI

@main
struct SvoyVPNApp: App {
    @StateObject private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
                .onOpenURL { url in
                    handleDeepLink(url)
                }
        }
    }
    
    private func handleDeepLink(_ url: URL) {
        // svoyvpn://auth?token=JWT
        guard url.scheme == AppConfig.appScheme,
              url.host == "auth",
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let tokenItem = components.queryItems?.first(where: { $0.name == "token" }),
              let token = tokenItem.value, !token.isEmpty else {
            return
        }
        
        TokenStorage.shared.saveToken(token)
        appState.isLoggedIn = true
    }
}

class AppState: ObservableObject {
    @Published var isLoggedIn: Bool = TokenStorage.shared.isLoggedIn
}

struct ContentView: View {
    @EnvironmentObject var appState: AppState

    var body: some View {
        if appState.isLoggedIn {
            WebViewContainer()
                .edgesIgnoringSafeArea(.all)
        } else {
            AuthViewContainer()
                .edgesIgnoringSafeArea(.all)
        }
    }
}

// SwiftUI Wrappers for UIKit controllers
struct WebViewContainer: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> WebViewViewController {
        return WebViewViewController()
    }
    
    func updateUIViewController(_ uiViewController: WebViewViewController, context: Context) {}
}

struct AuthViewContainer: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> AuthViewController {
        return AuthViewController()
    }
    
    func updateUIViewController(_ uiViewController: AuthViewController, context: Context) {}
}
