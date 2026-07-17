import XCTest
@testable import Bruce

/// Fake backend so the session's state machine is tested with no network. Records the idempotency
/// keys it received (to prove a retry never mints a new one) and serves a scripted mission sequence.
final class FakeIntakeAPI: IntakeAPI {
    var acceptResult: Result<IntakeAccepted, Error>
    var missionScript: [MissionDetail]
    private(set) var submittedKeys: [String] = []
    private(set) var submitCount = 0
    private var missionIndex = 0

    init(accept: Result<IntakeAccepted, Error>, missions: [MissionDetail] = []) {
        self.acceptResult = accept
        self.missionScript = missions
    }

    func submitIntakeText(_ text: String, idempotencyKey: String) async throws -> IntakeAccepted {
        submitCount += 1; submittedKeys.append(idempotencyKey)
        return try acceptResult.get()
    }
    func submitIntakeBytes(_ bytes: Data, mime: String, sourceKind: String, idempotencyKey: String) async throws -> IntakeAccepted {
        submitCount += 1; submittedKeys.append(idempotencyKey)
        return try acceptResult.get()
    }
    func mission(_ id: UUID) async throws -> MissionDetail {
        defer { missionIndex = min(missionIndex + 1, missionScript.count - 1) }
        guard !missionScript.isEmpty else { throw BruceAPIError.badStatus(500) }
        return missionScript[missionIndex]
    }
}

// MARK: - builders

private func accepted() -> IntakeAccepted {
    IntakeAccepted(mission_id: UUID(), source_id: UUID(), state: "understanding",
                   display_status: "Understanding your flyer…", poll: [:])
}

private func mission(_ phase: String, extracted: ExtractedIntake? = nil, blocking: String? = nil) -> MissionDetail {
    MissionDetail(mission_id: UUID(), status: "running", phase: phase,
                  short_status: phase == "understanding" ? "Understanding your flyer…" : phase,
                  error: blocking, version: 1, extracted: extracted,
                  blocking_reason: blocking, available_actions: phase == "awaiting_approval" ? ["approve"] : [])
}

private func grounded() -> ExtractedIntake {
    ExtractedIntake(title: "Science Fair",
                    deadlines: [
                        ExtractedDeadline(label: "Registration", date: "2026-02-28", source_span: "Feb 28", confidence: 0.9),
                        ExtractedDeadline(label: "Judging", date: nil, source_span: "the following Friday", confidence: 0.5),
                    ],
                    required_items: [RequiredItem(name: "permission form", kind: "form", provided: false)],
                    cost: "$25", location: nil, eligibility: nil)
}

@MainActor
final class LiveIntakeSessionTests: XCTestCase {
    var events: [IntakeEvent] = []

    override func setUp() {
        events = []
        Analytics.sink = { [weak self] e in self?.events.append(e) }
        IntakeRestore.pending = nil
    }
    override func tearDown() { Analytics.sink = nil; IntakeRestore.pending = nil }

    private func wait(_ s: LiveIntakeSession, until: @escaping (LiveIntakeSession.Stage) -> Bool) async {
        for _ in 0..<500 { if until(s.stage) { return }; try? await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("timed out in stage \(s.stage)")
    }

    // 1. text submit -> acknowledged -> working, restore persisted
    func test_text_submit_acknowledges_and_persists_restore() async {
        let api = FakeIntakeAPI(accept: .success(accepted()), missions: [mission("understanding")])
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(text: "Applications due May 1", type: .text)
        await wait(s) { $0 == .working }
        XCTAssertNotNil(s.missionID)
        XCTAssertEqual(IntakeRestore.pending, s.missionID)
        XCTAssertTrue(events.contains(.submissionStarted(.text)))
        XCTAssertTrue(events.contains(.missionAcknowledged))
    }

    // 2. poll reaches ready -> completed + restore cleared
    func test_poll_reaches_ready_completes() async {
        let api = FakeIntakeAPI(accept: .success(accepted()),
                                missions: [mission("understanding"), mission("awaiting_approval", extracted: grounded())])
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(text: "x", type: .text)
        await wait(s) { $0 == .ready }
        XCTAssertNil(IntakeRestore.pending)
        XCTAssertTrue(events.contains(.extractionCompleted))
        XCTAssertEqual(s.groundedDeadlines.count, 1)   // Feb 28
        XCTAssertEqual(s.ambiguities.count, 1)         // "the following Friday"
    }

    // 3. terminal failure -> failed, restore cleared, no false completion
    func test_poll_reaches_failed() async {
        let api = FakeIntakeAPI(accept: .success(accepted()),
                                missions: [mission("understanding"), mission("failed", blocking: "unreadable")])
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(text: "x", type: .pdf)
        await wait(s) { $0 == .failed }
        XCTAssertNil(IntakeRestore.pending)
        XCTAssertTrue(events.contains(.extractionFailed(.unreadable)))
    }

    // 4. blocked (provider) stays recoverable
    func test_provider_blocked_is_recoverable() async {
        let api = FakeIntakeAPI(accept: .success(accepted()),
                                missions: [mission("blocked", blocking: "provider_unavailable — retrying")])
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(text: "x", type: .text)
        await wait(s) { $0 == .blocked }
        XCTAssertTrue(s.canRetry)
    }

    // 5. session expired on 401
    func test_session_expired_on_401() async {
        let api = FakeIntakeAPI(accept: .failure(BruceAPIError.badStatus(401)))
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(text: "x", type: .text)
        await wait(s) { $0 == .sessionExpired }
        XCTAssertTrue(events.contains(.extractionFailed(.sessionExpired)))
    }

    // 6. offline / network failure on submit
    func test_offline_submit_fails_recoverably() async {
        let api = FakeIntakeAPI(accept: .failure(URLError(.notConnectedToInternet)))
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(text: "x", type: .text)
        await wait(s) { $0 == .failed }
        XCTAssertTrue(s.canRetry)
        XCTAssertTrue(events.contains(.extractionFailed(.network)))
    }

    // 7. duplicate tap is ignored while busy
    func test_duplicate_submit_ignored_while_busy() async {
        let api = FakeIntakeAPI(accept: .success(accepted()), missions: [mission("understanding")])
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(text: "x", type: .text)
        s.submit(text: "x", type: .text)   // second tap
        await wait(s) { $0 == .working }
        XCTAssertEqual(api.submitCount, 1)
        XCTAssertEqual(events.filter { $0 == .submissionStarted(.text) }.count, 1)
    }

    // 8. retry reuses the SAME idempotency key -> never a second mission
    func test_retry_reuses_same_idempotency_key() async {
        let api = FakeIntakeAPI(accept: .failure(URLError(.timedOut)))
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(text: "x", type: .text)
        await wait(s) { $0 == .failed }
        api.acceptResult = .success(accepted()); api.missionScript = [mission("understanding")]
        s.retry()
        await wait(s) { $0 == .working }
        XCTAssertEqual(api.submittedKeys.count, 2)
        XCTAssertEqual(api.submittedKeys[0], api.submittedKeys[1])   // same key
        XCTAssertTrue(events.contains(.retryUsed))
    }

    // 9. resume polls an existing mission without resubmitting
    func test_resume_polls_without_resubmit() async {
        let api = FakeIntakeAPI(accept: .failure(URLError(.timedOut)),
                                missions: [mission("awaiting_approval", extracted: grounded())])
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.resume(missionID: UUID())
        await wait(s) { $0 == .ready }
        XCTAssertEqual(api.submitCount, 0)   // never submitted
    }

    // 10. bytes (image/PDF) dispatch through the byte path
    func test_bytes_submit_dispatches() async {
        let api = FakeIntakeAPI(accept: .success(accepted()), missions: [mission("understanding")])
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(bytes: Data([0x89, 0x50]), mime: "image/png", sourceKind: "image", type: .photo)
        await wait(s) { $0 == .working }
        XCTAssertEqual(api.submitCount, 1)
        XCTAssertTrue(events.contains(.submissionStarted(.photo)))
    }

    // 11. analytics are content-free: every attribute is a known enum raw value, never free text
    func test_analytics_are_content_free() async {
        let api = FakeIntakeAPI(accept: .success(accepted()),
                                missions: [mission("understanding"), mission("awaiting_approval", extracted: grounded())])
        let s = LiveIntakeSession(api: api, pollInterval: 0.001)
        s.submit(text: "SECRET essay draft and parent phone 555-0100", type: .text)
        await wait(s) { $0 == .ready }
        let allowedSources = Set(["photo", "screenshot", "pdf", "text", "link"])
        let allowedReasons = Set(["unreadable", "providerUnavailable", "sessionExpired", "network", "timeout"])
        let allowedStages = Set(["picking", "entering", "submitting", "working"])
        for e in events {
            if let a = e.attribute {
                XCTAssertTrue(allowedSources.union(allowedReasons).union(allowedStages).contains(a),
                              "unexpected (possibly content-bearing) attribute: \(a)")
            }
        }
    }

    // 12. IntakeRestore round-trips (app-kill restoration substrate)
    func test_intake_restore_roundtrips() {
        let id = UUID()
        IntakeRestore.pending = id
        XCTAssertEqual(IntakeRestore.pending, id)
        IntakeRestore.pending = nil
        XCTAssertNil(IntakeRestore.pending)
    }
}
