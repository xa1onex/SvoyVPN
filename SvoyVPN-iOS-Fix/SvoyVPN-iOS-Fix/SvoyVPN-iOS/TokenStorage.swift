import Foundation
import Security
import Combine

/// Securely stores JWT token and auth state in iOS Keychain.
/// Mirrors Android's TokenStorage using EncryptedSharedPreferences.
final class TokenStorage: ObservableObject {

    @Published var loggedInStatus: Bool
    
    static let shared = TokenStorage()
    private init() {
        self.loggedInStatus = false
        // Initialize loggedInStatus at startup
        self.loggedInStatus = self.isLoggedIn
    }

    private let service = "com.svoyvpn.app"
    private let tokenAccount = "jwt_token"
    private let userIdAccount = "user_id"

    // MARK: – JWT Token

    func saveToken(_ token: String) {
        save(value: token, account: tokenAccount)
        DispatchQueue.main.async {
            self.loggedInStatus = true
            self.objectWillChange.send()
        }
    }

    func getToken() -> String? {
        return load(account: tokenAccount)
    }

    func clearToken() {
        delete(account: tokenAccount)
        delete(account: userIdAccount)
        DispatchQueue.main.async {
            self.loggedInStatus = false
            self.objectWillChange.send()
        }
    }

    var isLoggedIn: Bool {
        guard let t = getToken() else { return false }
        return !t.isEmpty
    }

    // MARK: – User ID

    func saveUserId(_ userId: Int64) {
        save(value: String(userId), account: userIdAccount)
    }

    func getUserId() -> Int64 {
        guard let s = load(account: userIdAccount), let id = Int64(s) else { return -1 }
        return id
    }

    // MARK: – Keychain helpers

    private func save(value: String, account: String) {
        guard let data = value.data(using: .utf8) else { return }
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account
        ]
        SecItemDelete(query as CFDictionary)
        let attrs: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account,
            kSecValueData: data,
            kSecAttrAccessible: kSecAttrAccessibleAfterFirstUnlock
        ]
        SecItemAdd(attrs as CFDictionary, nil)
    }

    private func load(account: String) -> String? {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account,
            kSecReturnData: true,
            kSecMatchLimit: kSecMatchLimitOne
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess,
              let data = result as? Data,
              let str = String(data: data, encoding: .utf8) else { return nil }
        return str
    }

    private func delete(account: String) {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account
        ]
        SecItemDelete(query as CFDictionary)
    }
}
