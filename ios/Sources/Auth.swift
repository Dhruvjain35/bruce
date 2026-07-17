import SwiftUI
import Observation
import AuthenticationServices
import CryptoKit
import Security
import UIKit

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
    case cancelled, noIdentityToken, exchangeFailed(Int), network, notConfigured
    var errorDescription: String? {
        switch self {
        case .cancelled: return "Sign-in was cancelled."
        case .noIdentityToken: return "Apple didn't return an identity token. Try again."
        case .exchangeFailed(let c): return c == 401 ? "Apple sign-in couldn't be verified." : "Couldn't sign you in (\(c))."
        case .network: return "Couldn't reach Bruce. Check your connection."
        case .notConfigured: return "Sign in with Apple isn't configured for this build."
        }
    }
}

/// App-wide identity. Holds the Bruce session JWT (Keychain-backed) and drives Sign in with Apple.
/// The token's subject is derived server-side from Apple's stable id — the client never sets a user id.
@Observable final class AppSession {
    static let shared = AppSession()

    private(set) var token: String?
    private(set) var userID: UUID?
    var lastError: AuthError? = nil

    /// The bearer the API layer sends. Real token first; the dev token only if DEBUG+flag allow it.
    var bearer: String? { token ?? AppConfig.devToken }
    var isSignedIn: Bool { token != nil }

    private let coordinator = AppleSignInCoordinator()

    private init() { token = Keychain.read() }

    @MainActor func signInWithApple() async -> Bool {
        lastError = nil
        do {
            let (idToken, rawNonce) = try await coordinator.run()
            try await exchange(idToken: idToken, rawNonce: rawNonce)
            return true
        } catch let e as AuthError {
            if e != .cancelled { lastError = e }   // cancellation is not an error to surface
            return false
        } catch {
            lastError = .network
            return false
        }
    }

    func signOut() {
        Keychain.clear(); token = nil; userID = nil
    }

    private func exchange(idToken: String, rawNonce: String) async throws {
        var req = URLRequest(url: AppConfig.baseURL.appending(path: "/v1/auth/apple"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["identity_token": idToken, "raw_nonce": rawNonce])
        let (data, resp): (Data, URLResponse)
        do { (data, resp) = try await URLSession.shared.data(for: req) }
        catch { throw AuthError.network }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
        guard code == 200 else { throw AuthError.exchangeFailed(code) }
        struct SessionToken: Codable { let token: String; let user_id: UUID; let expires_in: Int }
        let s = try JSONDecoder().decode(SessionToken.self, from: data)
        await MainActor.run {
            Keychain.save(s.token)
            self.token = s.token
            self.userID = s.user_id
        }
    }
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

/// Runs the native Sign in with Apple request and returns (identityToken, rawNonce). Uses a fresh
/// random nonce each time (bound into the token, checked server-side against replay).
final class AppleSignInCoordinator: NSObject, ASAuthorizationControllerDelegate, ASAuthorizationControllerPresentationContextProviding {
    private var cont: CheckedContinuation<(String, String), Error>?
    private var rawNonce = ""

    @MainActor func run() async throws -> (String, String) {
        rawNonce = AppleNonce.make()
        let req = ASAuthorizationAppleIDProvider().createRequest()
        req.requestedScopes = [.fullName, .email]
        req.nonce = AppleNonce.sha256(rawNonce)   // Apple echoes this hash in the token's nonce claim
        let controller = ASAuthorizationController(authorizationRequests: [req])
        controller.delegate = self
        controller.presentationContextProvider = self
        return try await withCheckedThrowingContinuation { c in
            self.cont = c
            controller.performRequests()
        }
    }

    func authorizationController(controller: ASAuthorizationController, didCompleteWithAuthorization authorization: ASAuthorization) {
        guard let cred = authorization.credential as? ASAuthorizationAppleIDCredential,
              let tokenData = cred.identityToken, let idToken = String(data: tokenData, encoding: .utf8) else {
            cont?.resume(throwing: AuthError.noIdentityToken); cont = nil; return
        }
        cont?.resume(returning: (idToken, rawNonce)); cont = nil
    }

    func authorizationController(controller: ASAuthorizationController, didCompleteWithError error: Error) {
        if let e = error as? ASAuthorizationError, e.code == .canceled {
            cont?.resume(throwing: AuthError.cancelled)
        } else {
            cont?.resume(throwing: AuthError.network)
        }
        cont = nil
    }

    func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }.flatMap(\.windows).first { $0.isKeyWindow } ?? ASPresentationAnchor()
    }
}
