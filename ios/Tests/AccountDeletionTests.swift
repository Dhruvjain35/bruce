import XCTest
@testable import Bruce

/// Injectable transport so account deletion is tested with no server.
final class FakeAuthTransport: AuthTransport {
    enum Mode { case ok, throwing(Error), gated }
    var mode: Mode
    private(set) var deleteCalls = 0
    private var gate: CheckedContinuation<Void, Never>?

    init(_ mode: Mode) { self.mode = mode }

    func appleExchange(idToken: String, rawNonce: String) async throws -> (token: String, userID: UUID) {
        ("jwt", UUID())
    }
    func deleteAccount(bearer: String) async throws {
        deleteCalls += 1
        switch mode {
        case .ok: return
        case .throwing(let e): throw e
        case .gated: await withCheckedContinuation { gate = $0 }
        }
    }
    func release() { gate?.resume(); gate = nil }
}

@MainActor
final class AccountDeletionTests: XCTestCase {
    override func setUp() { Keychain.clear() }
    override func tearDown() { Keychain.clear() }

    // success -> server confirmed -> local cleared -> signed out
    func test_delete_success_clears_local_and_signs_out() async {
        let t = FakeAuthTransport(.ok)
        let s = AppSession(transport: t, seedToken: "jwt")
        XCTAssertTrue(s.isSignedIn)
        let ok = await s.deleteAccount()
        XCTAssertTrue(ok)
        XCTAssertFalse(s.isSignedIn)
        XCTAssertNil(s.token)
        XCTAssertEqual(t.deleteCalls, 1)
    }

    // network failure -> recoverable -> token retained (deletion NOT claimed)
    func test_delete_network_failure_is_recoverable() async {
        let t = FakeAuthTransport(.throwing(AuthError.network))
        let s = AppSession(transport: t, seedToken: "jwt")
        let ok = await s.deleteAccount()
        XCTAssertFalse(ok)
        XCTAssertTrue(s.isSignedIn)       // still signed in — server never confirmed
        XCTAssertEqual(s.lastError, .network)
    }

    // expired auth (server 401) -> not cleared, never claims success
    func test_delete_expired_auth_does_not_claim_success() async {
        let t = FakeAuthTransport(.throwing(AuthError.exchangeFailed(401)))
        let s = AppSession(transport: t, seedToken: "jwt")
        let ok = await s.deleteAccount()
        XCTAssertFalse(ok)
        XCTAssertTrue(s.isSignedIn)
        XCTAssertEqual(s.lastError, .exchangeFailed(401))
    }

    // no local token -> not signed in -> never hits the network
    func test_delete_without_token_never_calls_server() async {
        let t = FakeAuthTransport(.ok)
        let s = AppSession(transport: t, seedToken: nil)   // Keychain cleared in setUp
        let ok = await s.deleteAccount()
        XCTAssertFalse(ok)
        XCTAssertEqual(t.deleteCalls, 0)
        XCTAssertEqual(s.lastError, .notSignedIn)
    }

    // repeated taps -> exactly one server delete; the second is ignored while in flight
    func test_repeated_taps_delete_once() async {
        let t = FakeAuthTransport(.gated)
        let s = AppSession(transport: t, seedToken: "jwt")
        let t1 = Task { await s.deleteAccount() }
        await Task.yield()                 // let t1 reach the gated transport
        let t2 = Task { await s.deleteAccount() }
        await Task.yield()                 // t2 sees isDeleting == true and bails
        t.release()
        let r1 = await t1.value, r2 = await t2.value
        XCTAssertEqual(t.deleteCalls, 1)
        XCTAssertNotEqual(r1, r2)           // one succeeded, one was ignored
    }
}
