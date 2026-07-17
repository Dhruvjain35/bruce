import SwiftUI
import Observation

/// Persists the one in-flight intake mission so a killed app can restore it on relaunch. Stored id
/// only — never any document content.
enum IntakeRestore {
    private static let key = "bruce.activeIntakeMission"
    static var pending: UUID? {
        get { UserDefaults.standard.string(forKey: key).flatMap(UUID.init) }
        set {
            if let v = newValue { UserDefaults.standard.set(v.uuidString, forKey: key) }
            else { UserDefaults.standard.removeObject(forKey: key) }
        }
    }
}

/// Drives the capture → durable-mission → poll journey behind the designed HandoffSheet. Reflects the
/// real backend; owns no mock success. The 202 comes back in ~50ms, so "Understanding…" is honest and
/// immediate; the screen reconciles to grounded results (or a real failure) when the worker finishes.
@Observable final class LiveIntakeSession {
    enum Stage: Equatable { case idle, submitting, working, ready, blocked, failed, sessionExpired }

    var stage: Stage = .idle
    var displayStatus = ""
    var missionID: UUID? = nil
    var sourceID: UUID? = nil
    var extracted: ExtractedIntake? = nil
    var blockingReason: String? = nil
    var uploading = false                 // indeterminate upload feedback (photo/PDF submit)
    private(set) var sourceType: IntakeEvent.SourceType = .text

    private let api: IntakeAPI
    private let pollInterval: TimeInterval
    private var idempotencyKey = UUID().uuidString   // one per capture; reused on retry -> no dup mission
    private var pollTask: Task<Void, Never>? = nil
    private var lastSubmit: (() async throws -> IntakeAccepted)? = nil

    init(api: IntakeAPI = BruceAPI(), pollInterval: TimeInterval = 1.0) {
        self.api = api
        self.pollInterval = pollInterval
    }

    var isBusy: Bool { stage == .submitting || stage == .working }
    var canRetry: Bool { stage == .blocked || stage == .failed || stage == .sessionExpired }

    /// The ambiguous deadlines the server refused to guess a date for — surfaced as real Decisions.
    var ambiguities: [ExtractedDeadline] { (extracted?.deadlines ?? []).filter { $0.isAmbiguous } }
    var groundedDeadlines: [ExtractedDeadline] { (extracted?.deadlines ?? []).filter { !$0.isAmbiguous } }

    func submit(text: String, type: IntakeEvent.SourceType) {
        guard stage == .idle || canRetry else { return }   // dup-tap / double-submit guard
        sourceType = type
        Analytics.track(.submissionStarted(type))
        run { try await self.api.submitIntakeText(text, idempotencyKey: self.idempotencyKey) }
    }

    func submit(bytes: Data, mime: String, sourceKind: String, type: IntakeEvent.SourceType) {
        guard stage == .idle || canRetry else { return }
        sourceType = type
        uploading = true
        Analytics.track(.submissionStarted(type))
        run { try await self.api.submitIntakeBytes(bytes, mime: mime, sourceKind: sourceKind, idempotencyKey: self.idempotencyKey) }
    }

    /// Re-poll an existing mission (return visit or app relaunch) without resubmitting.
    func resume(missionID id: UUID) {
        missionID = id
        stage = .working
        displayStatus = "Understanding what you sent…"
        startPolling(id)
    }

    func retry() {
        guard canRetry, let call = lastSubmit else { return }
        Analytics.track(.retryUsed)
        run(call)   // same idempotency key -> backend returns the existing mission; never duplicates
    }

    /// Cancel BEFORE submission (or stop polling). Safe at any time.
    func cancel() {
        pollTask?.cancel(); pollTask = nil
        uploading = false
        if stage == .idle || stage == .submitting { stage = .idle }
    }

    private func run(_ call: @escaping () async throws -> IntakeAccepted) {
        lastSubmit = call
        stage = .submitting
        displayStatus = "Sending…"
        Task { await self._submit(call) }
    }

    private func _submit(_ call: @escaping () async throws -> IntakeAccepted) async {
        do {
            let accepted = try await call()
            Analytics.track(.missionAcknowledged)
            await MainActor.run {
                self.uploading = false
                self.missionID = accepted.mission_id
                self.sourceID = accepted.source_id
                self.displayStatus = accepted.display_status
                self.stage = .working
                IntakeRestore.pending = accepted.mission_id   // survive an app kill
            }
            startPolling(accepted.mission_id)
        } catch {
            await MainActor.run { self.fail(from: error) }
        }
    }

    @MainActor private func fail(from error: Error) {
        uploading = false
        if case BruceAPIError.badStatus(401) = error {
            stage = .sessionExpired
            blockingReason = "Your session expired. Sign in again to keep going."
            Analytics.track(.extractionFailed(.sessionExpired))
        } else {
            stage = .failed
            blockingReason = "Couldn't reach Bruce. Check your connection and try again."
            Analytics.track(.extractionFailed(.network))
        }
    }

    private func startPolling(_ id: UUID) {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            guard let self else { return }
            for _ in 0..<40 {   // ~40s ceiling; backend's 20s budget resolves well inside this
                if Task.isCancelled { return }
                if let m = try? await self.api.mission(id) {
                    await MainActor.run { self.apply(m) }
                    if !m.isWorking { return }
                }
                try? await Task.sleep(nanoseconds: UInt64(pollInterval * 1_000_000_000))
            }
        }
    }

    @MainActor private func apply(_ m: MissionDetail) {
        displayStatus = m.short_status
        extracted = m.extracted
        blockingReason = m.blocking_reason
        if m.isReady {
            stage = .ready; IntakeRestore.pending = nil
            Analytics.track(.extractionCompleted)
        } else if m.isBlocked {
            stage = .blocked
        } else if m.isTerminalFailure {
            stage = .failed; IntakeRestore.pending = nil
            let reason: IntakeEvent.Reason = (m.blocking_reason ?? "").lowercased().contains("provider") ? .providerUnavailable : .unreadable
            Analytics.track(.extractionFailed(reason))
        } else {
            stage = .working
        }
    }

    /// Called when the sheet is dismissed before the mission was acknowledged/ready.
    func trackAbandon() {
        let s: IntakeEvent.Stage
        switch stage {
        case .idle: s = .picking
        case .submitting: s = .submitting
        case .working: s = .working
        default: return   // ready/failed aren't abandonment
        }
        Analytics.track(.userAbandoned(s))
    }
}
