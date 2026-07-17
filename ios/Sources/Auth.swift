import SwiftUI
import Observation
import AuthenticationServices
import CryptoKit
import Security

// MARK: - Config

/// Where the app talks to the engine, and whether the local dev token is permitted.
enum AppConfig {
    /// Override with the BRUCE_API_BASE env var (set per scheme / deployment). Defaults to the local
    /// engine so a dev build works out of the box.
    static var baseURL: URL {
        if let s = ProcessInfo.processInfo.environment["BRUCE_API_BASE"], let u = URL(string: s) { return u }
        return URL(string: "http://127.0.0.1:8000")!
    }

    /// The DEV token is compiled in ONLY for DEBUG builds AND only when explicitly enabled, so a
    /// release/TestFlight build can never silently fall back to it — it requires real Sign in with Apple.
    static var devToken: String? {
        #if DEBUG
        guard ProcessInfo.processInfo.environment["BRUCE_DEV_AUTH"] == "1" else { return nil }
        // Long-lived HS256 token signed with the local dev secret (sub = the dev user).
        return "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTExMTEtMTExMS0xMTExMTExMTExMTEiLCJleHAiOjE4MTU1MjMzOTcsImF1ZCI6ImF1dGhlbnRpY2F0ZWQifQ.h1HeZ2MT9s0ZlaKOzY-RC-icdR4gJF-sJMR_P6ug--k"
        #else
        return nil
        #endif
    }
}

// MARK: - Keychain (store the Bruce session JWT)

enum Keychain {
    private static let account = "bruce.session.jwt"
    static func save(_ value: String) {
        let data = Data(value.utf8)
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account]
        SecItemDelete(q as CFDictionary)
        var add = q; add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }
    static func read() -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account,
                                kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }
    static func clear() {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account] as CFDictionary)
    }
}

// MARK: - Session

enum AuthError: LocalizedError, Equatable {
    case cancelled, noIdentityToken, exchangeFailed(Int), network, notConfigured, notSignedIn
    var errorDescription: String? {
        switch self {
        case .cancelled: return "Sign-in was cancelled."
        case .noIdentityToken: return "Apple didn't return an identity token. Try again."
        case .exchangeFailed(let c): return c == 401 ? "Your session expired. Sign in again." : "Something went wrong (\(c))."
        case .network: return "Couldn't reach Bruce. Check your connection and try again."
        case .notConfigured: return "Sign in with Apple isn't configured for this build."
        case .notSignedIn: return "You're not signed in."
        }
    }
}

/// Network operations for auth/account — injectable so AppSession is unit-testable without a server.
protocol AuthTransport {
    func appleExchange(idToken: String, rawNonce: String) async throws -> (token: String, userID: UUID)
    func deleteAccount(bearer: String) async throws
}

struct URLSessionAuthTransport: AuthTransport {
    func appleExchange(idToken: String, rawNonce: String) async throws -> (token: String, userID: UUID) {
        var req = URLRequest(url: AppConfig.baseURL.appending(path: "/v1/auth/apple"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["identity_token": idToken, "raw_nonce": rawNonce])
        let (data, resp): (Data, URLResponse)
        do { (data, resp) = try await URLSession.shared.data(for: req) } catch { throw AuthError.network }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
        guard code == 200 else { throw AuthError.exchangeFailed(code) }
        struct SessionToken: Codable { let token: String; let user_id: UUID; let expires_in: Int }
        let s = try JSONDecoder().decode(SessionToken.self, from: data)
        return (s.token, s.user_id)
    }

    func deleteAccount(bearer: String) async throws {
        var req = URLRequest(url: AppConfig.baseURL.appending(path: "/v1/account"))
        req.httpMethod = "DELETE"
        req.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        let resp: URLResponse
        do { (_, resp) = try await URLSession.shared.data(for: req) } catch { throw AuthError.network }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
        guard (200..<300).contains(code) else { throw AuthError.exchangeFailed(code) }
    }
}

/// App-wide identity. Holds the Bruce session JWT (Keychain-backed) and drives Sign in with Apple.
/// The token's subject is derived server-side from Apple's stable id — the client never sets a user id.
@Observable final class AppSession {
    static let shared = AppSession()

    private(set) var token: String?
    private(set) var userID: UUID?
    var lastError: AuthError? = nil
    private(set) var isDeleting = false

    private let transport: AuthTransport
    private var rawNonce = ""

    /// The bearer the API layer sends. Real token first; the dev token only if DEBUG+flag allow it.
    var bearer: String? { token ?? AppConfig.devToken }
    var isSignedIn: Bool { token != nil }

    init(transport: AuthTransport = URLSessionAuthTransport(), seedToken: String? = nil) {
        self.transport = transport
        token = seedToken ?? Keychain.read()   // seedToken is a test seam; prod reads Keychain
    }

    // MARK: Sign in with Apple (driven by the official SignInWithAppleButton)

    /// Configure the button's request: a FRESH random nonce each attempt (its sha256 is bound into
    /// the token and checked server-side against replay).
    func prepare(_ request: ASAuthorizationAppleIDRequest) {
        rawNonce = AppleNonce.make()
        request.requestedScopes = [.fullName, .email]
        request.nonce = AppleNonce.sha256(rawNonce)
    }

    /// Handle the button's completion. Returns true on a verified sign-in.
    @MainActor func complete(_ result: Result<ASAuthorization, Error>) async -> Bool {
        lastError = nil
        switch result {
        case .failure(let error):
            if let e = error as? ASAuthorizationError, e.code == .canceled { return false }  // silent
            lastError = .network
            return false
        case .success(let auth):
            guard let cred = auth.credential as? ASAuthorizationAppleIDCredential,
                  let data = cred.identityToken, let idToken = String(data: data, encoding: .utf8) else {
                lastError = .noIdentityToken; return false
            }
            do {
                let (tok, uid) = try await transport.appleExchange(idToken: idToken, rawNonce: rawNonce)
                Keychain.save(tok); token = tok; userID = uid
                return true
            } catch let e as AuthError { lastError = e; return false }
            catch { lastError = .network; return false }
        }
    }

    func signOut() { clearLocal(); token = nil; userID = nil }

    // MARK: Account deletion

    /// Delete the account server-side, then locally. Returns true ONLY after the server confirms —
    /// never claims success early. Guarded against repeated taps.
    @MainActor func deleteAccount() async -> Bool {
        guard !isDeleting else { return false }
        guard let bearer = token else { lastError = .notSignedIn; return false }  // expired/no auth
        isDeleting = true
        defer { isDeleting = false }
        do {
            try await transport.deleteAccount(bearer: bearer)   // throws on non-2xx / network
        } catch let e as AuthError { lastError = e; return false }
        catch { lastError = .network; return false }
        // Server confirmed. Now clear everything local and drop to signed-out.
        clearLocal(); token = nil; userID = nil
        return true
    }

    /// Clear all local user-specific state: Keychain JWT, restore pointer, caches, Live Activities.
    private func clearLocal() {
        Keychain.clear()
        IntakeRestore.pending = nil
        LiveActivities.endAll()
    }
}

/// Live Activities teardown. No ActivityKit surface exists yet, so this is a safe no-op that becomes
/// the single place to end activities the moment one is added (e.g. an intake Dynamic Island).
enum LiveActivities {
    static func endAll() { /* no ActivityKit activities yet — hook for when one lands */ }
}

// MARK: - Apple flow (nonce + ASAuthorizationController)

enum AppleNonce {
    static func make(length: Int = 32) -> String {
        let chars = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._")
        var bytes = [UInt8](repeating: 0, count: length)
        _ = SecRandomCopyBytes(kSecRandomDefault, length, &bytes)
        return String(bytes.map { chars[Int($0) % chars.count] })
    }
    static func sha256(_ input: String) -> String {
        SHA256.hash(data: Data(input.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}
