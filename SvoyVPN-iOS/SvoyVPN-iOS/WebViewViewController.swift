import UIKit
import WebKit

final class WebViewViewController: UIViewController {

    private var webView: WKWebView!
    
    override func viewDidLoad() {
        super.viewDidLoad()
        
        let isDark = traitCollection.userInterfaceStyle == .dark
        view.backgroundColor = isDark ? UIColor(hex: "#18222d") : .white
        
        setupWebView()
        loadMiniApp()
    }
    
    override func traitCollectionDidChange(_ previousTraitCollection: UITraitCollection?) {
        super.traitCollectionDidChange(previousTraitCollection)
        if traitCollection.hasDifferentColorAppearance(comparedTo: previousTraitCollection) {
            let isDark = traitCollection.userInterfaceStyle == .dark
            view.backgroundColor = isDark ? UIColor(hex: "#18222d") : .white
            webView.backgroundColor = view.backgroundColor
            
            // Re-inject theme dynamically if needed
            let colorScheme = isDark ? "dark" : "light"
            let bgColor = isDark ? "#18222d" : "#ffffff"
            let secondaryBg = isDark ? "#21303f" : "#f7f9fb"
            let textColor = isDark ? "#ffffff" : "#000000"
            let hintColor = isDark ? "#8e9db0" : "#999999"
            
            let js = """
                if (window.Telegram && window.Telegram.WebApp) {
                    window.Telegram.WebApp.colorScheme = '\(colorScheme)';
                    window.Telegram.WebApp.themeParams = {
                        bg_color: '\(bgColor)',
                        secondary_bg_color: '\(secondaryBg)',
                        text_color: '\(textColor)',
                        hint_color: '\(hintColor)',
                        accent_text_color: '#3aa8fc'
                    };
                }
                document.documentElement.setAttribute('data-theme', '\(colorScheme)');
                document.body.setAttribute('data-theme', '\(colorScheme)');
                var meta = document.querySelector('meta[name="theme-color"]');
                if (meta) meta.setAttribute('content', '\(bgColor)');
            """
            webView.evaluateJavaScript(js, completionHandler: nil)
        }
    }

    private func setupWebView() {
        let isDark = traitCollection.userInterfaceStyle == .dark
        
        let controller = WKUserContentController()
        controller.add(NativeBridgeHandler(owner: self), name: "iOSBridge")

        let config = WKWebViewConfiguration()
        config.userContentController = controller
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []

        let prefs = WKWebpagePreferences()
        prefs.allowsContentJavaScript = true
        config.defaultWebpagePreferences = prefs

        webView = WKWebView(frame: .zero, configuration: config)
        webView.scrollView.bounces = false // Disable rubber-banding
        webView.scrollView.isScrollEnabled = true
        webView.navigationDelegate = self
        // Set background matching mini-app theme to avoid white flash
        webView.backgroundColor = isDark ? UIColor(hex: "#18222d") : .white
        webView.isOpaque = true
        webView.translatesAutoresizingMaskIntoConstraints = false

        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor)
        ])
    }

    private func loadMiniApp() {
        guard let token = TokenStorage.shared.getToken() else {
            logout()
            return
        }
        
        let isDark = traitCollection.userInterfaceStyle == .dark
        let themeMode = isDark ? "dark" : "light"
        
        var urlComps = URLComponents(string: AppConfig.miniAppURL)
        urlComps?.queryItems = [
            URLQueryItem(name: "jwt", value: token),
            URLQueryItem(name: "theme", value: themeMode)
        ]
        
        if let url = urlComps?.url {
            let request = URLRequest(url: url)
            webView.load(request)
        }
    }

    private func injectBridge() {
        guard let token = TokenStorage.shared.getToken() else { return }
        let safeToken = token.replacingOccurrences(of: "\"", with: "")
        let isDark = traitCollection.userInterfaceStyle == .dark
        let colorScheme = isDark ? "dark" : "light"
        let bgColor = isDark ? "#18222d" : "#ffffff"
        let secondaryBg = isDark ? "#21303f" : "#f7f9fb"
        let textColor = isDark ? "#ffffff" : "#000000"
        let hintColor = isDark ? "#8e9db0" : "#999999"

        let js = """
            (function() {
                window.__androidJwt = "\(safeToken)"; // We use the same name as Android to avoid changing web code
                window.__iosJwt = "\(safeToken)";
                
                // Mock Telegram.WebApp
                if (!window.Telegram) window.Telegram = {};
                if (!window.Telegram.WebApp) {
                    window.Telegram.WebApp = {
                        colorScheme: "\(colorScheme)",
                        themeParams: {
                            bg_color: "\(bgColor)",
                            secondary_bg_color: "\(secondaryBg)",
                            text_color: "\(textColor)",
                            hint_color: "\(hintColor)",
                            accent_text_color: "#3aa8fc"
                        },
                        initData: "",
                        initDataUnsafe: { user: null },
                        ready: function() {},
                        expand: function() {},
                        onEvent: function() {},
                        setHeaderColor: function() {},
                        setBackgroundColor: function() {},
                        setBottomBarColor: function() {},
                        openLink: function(url) { window.open(url, '_blank'); },
                        openInvoice: function(url) { window.open(url, '_blank'); },
                        HapticFeedback: { impactOccurred: function() {} }
                    };
                }
                
                var scheme = "\(colorScheme)";
                document.documentElement.setAttribute('data-theme', scheme);
                document.body.setAttribute('data-theme', scheme);
                var meta = document.querySelector('meta[name="theme-color"]');
                if (meta) meta.setAttribute('content', "\(bgColor)");
                
                if (typeof loadUser === 'function') loadUser();
                
                window.__androidLogout = function() {
                    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.iOSBridge) {
                        window.webkit.messageHandlers.iOSBridge.postMessage({action: 'logout'});
                    } else if (window.AndroidBridge) {
                        AndroidBridge.logout();
                    }
                };
            })();
        """
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    func logout() {
        TokenStorage.shared.clearToken()
        DispatchQueue.main.async {
            let vc = AuthViewController()
            vc.modalPresentationStyle = .fullScreen
            
            // Allow presenting Auth from root
            if let windowScene = UIApplication.shared.connectedScenes.first as? UIWindowScene,
               let window = windowScene.windows.first {
                window.rootViewController = vc
                window.makeKeyAndVisible()
            } else {
                self.present(vc, animated: true)
            }
        }
    }
    
    func share(text: String) {
        let activityVC = UIActivityViewController(activityItems: [text], applicationActivities: nil)
        DispatchQueue.main.async {
            self.present(activityVC, animated: true)
        }
    }
}

// MARK: - WKNavigationDelegate
extension WebViewViewController: WKNavigationDelegate {
    
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url {
            let host = url.host ?? ""
            if !AppConfig.allowedHosts.contains(host) && !url.absoluteString.starts(with: "about:") {
                // Open external links in Safari
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
                return
            }
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // Inject JWT and theme AFTER page has loaded
        injectBridge()
    }
}

// MARK: - JS -> Native Bridge
private final class NativeBridgeHandler: NSObject, WKScriptMessageHandler {
    weak var owner: WebViewViewController?
    init(owner: WebViewViewController) { self.owner = owner }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "iOSBridge",
              let body = message.body as? [String: Any],
              let action = body["action"] as? String else { return }
        
        if action == "logout" {
            DispatchQueue.main.async {
                let alert = UIAlertController(title: "Выход из аккаунта", message: "Вы уверены, что хотите выйти?", preferredStyle: .alert)
                alert.addAction(UIAlertAction(title: "Выйти", style: .destructive, handler: { _ in
                    self.owner?.logout()
                }))
                alert.addAction(UIAlertAction(title: "Отмена", style: .cancel, handler: nil))
                self.owner?.present(alert, animated: true, completion: nil)
            }
        } else if action == "share", let text = body["text"] as? String {
            owner?.share(text: text)
        } else if action == "getToken" {
            // Note: synchronous return to JS is not natively supported in WKScriptMessageHandler.
            // But JS in the mini-app likely uses window.__androidJwt directly.
        }
    }
}
