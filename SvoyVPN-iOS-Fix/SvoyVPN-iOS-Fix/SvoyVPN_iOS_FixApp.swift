import SwiftUI

@main
struct SvoyVPN_iOS_FixApp: App {
    @StateObject private var tokenStorage = TokenStorage.shared

    var body: some Scene {
        WindowGroup {
            if tokenStorage.loggedInStatus {
                WebViewContainer()
                    .edgesIgnoringSafeArea(.all)
            } else {
                AuthContainer()
                    .edgesIgnoringSafeArea(.all)
            }
        }
    }
}

struct WebViewContainer: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> WebViewViewController {
        return WebViewViewController()
    }

    func updateUIViewController(_ uiViewController: WebViewViewController, context: Context) {}
}

struct AuthContainer: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> AuthViewController {
        return AuthViewController()
    }

    func updateUIViewController(_ uiViewController: AuthViewController, context: Context) {}
}
