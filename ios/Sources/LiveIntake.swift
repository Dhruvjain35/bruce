import SwiftUI
import Observation

/// Bridges the capture UI to the async intake backend: submit -> durable mission -> poll canonical
/// state. The student sees "Understanding your flyer…" within a moment (the 202 comes back in ~50ms)
/// and the screen reconciles to real extracted objects when the worker finishes — never blocking.
///
/// Durability & safety are the backend's; this session just reflects it:
///  * one idempotency key per capture, reused on retry -> a resubmit never spawns a second mission.
///  * missionID is exposed so the app can persist it and re-poll after an app restart.
///  * a blocked mission offers retry; a failed one says so honestly (no fake completion).
@Observable final class LiveIntakeSession {
    enum Stage: Equatable { case idle, submitting, working, ready, blocked, failed }

    var stage: Stage = .idle
    var displayStatus: String = ""
    var missionID: UUID? = nil
    var extracted: ExtractedIntake? = nil
    var blockingReason: String? = nil

    private let api = BruceAPI()
    private var idempotencyKey = UUID().uuidString
    private var pollTask: Task<Void, Never>? = nil

    var isSubmitting: Bool { stage == .submitting }
    var canRetry: Bool { stage == .blocked || stage == .failed }

    /// Submit pasted/typed text. Guarded so a double-tap can't fire two submits.
    func submit(text: String) {
        guard stage == .idle || canRetry else { return }
        stage = .submitting
        displayStatus = "Sending…"
        Task { await self._submit { try await self.api.submitIntakeText(text, idempotencyKey: self.idempotencyKey) } }
    }

    /// Submit a captured flyer/screenshot (image) or PDF.
    func submit(bytes: Data, mime: String, sourceKind: String) {
        guard stage == .idle || canRetry else { return }
        stage = .submitting
        displayStatus = "Sending…"
        Task { await self._submit { try await self.api.submitIntakeBytes(bytes, mime: mime, sourceKind: sourceKind, idempotencyKey: self.idempotencyKey) } }
    }

    /// Re-poll an existing mission (e.g. after an app relaunch) without resubmitting.
    func resume(missionID id: UUID) {
        missionID = id
        stage = .working
        displayStatus = "Understanding what you sent…"
        startPolling(id)
    }

    func retry() {
        guard canRetry else { return }
        // Same idempotency key: if the mission already exists the backend returns it, so retry is
        // safe and idempotent — it never duplicates a source, task, or mission.
        if let id = missionID { stage = .working; startPolling(id) }
    }

    func cancel() {
        pollTask?.cancel()
        pollTask = nil
    }

    private func _submit(_ call: @escaping () async throws -> IntakeAccepted) async {
        do {
            let accepted = try await call()
            await MainActor.run {
                self.missionID = accepted.mission_id
                self.displayStatus = accepted.display_status
                self.stage = .working
            }
            startPolling(accepted.mission_id)
        } catch {
            await MainActor.run {
                self.stage = .failed
                self.blockingReason = "Couldn't reach Bruce. Check your connection and try again."
            }
        }
    }

    private func startPolling(_ id: UUID) {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            guard let self else { return }
            // Poll ~1s until the mission leaves a working phase. Bounded so a stuck mission can't
            // spin forever; the backend's 20s budget means it resolves well within this.
            for _ in 0..<40 {
                if Task.isCancelled { return }
                if let m = try? await self.api.mission(id) {
                    await MainActor.run { self.apply(m) }
                    if !m.isWorking { return }
                }
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    @MainActor private func apply(_ m: MissionDetail) {
        displayStatus = m.short_status
        extracted = m.extracted
        blockingReason = m.blocking_reason
        if m.isReady { stage = .ready }
        else if m.isBlocked { stage = .blocked }
        else if m.isTerminalFailure { stage = .failed }
        else { stage = .working }
    }
}

// MARK: - Capture sheet (paste/type today; image/PDF pickers slot into the same session)

struct LiveIntakeSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var session = LiveIntakeSession()
    @State private var text = ""

    var body: some View {
        ZStack {
            Theme.Backdrop()
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    switch session.stage {
                    case .idle, .submitting: composer
                    case .working:           workingCard
                    case .ready:             resultCard
                    case .blocked, .failed:  problemCard
                    }
                    Color.clear.frame(height: 40)
                }
                .padding(.horizontal, 20).padding(.top, 12)
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Add to Bruce").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
            Text("Paste an email, a deadline, or anything school-related. Bruce reads it and tracks it.")
                .font(.subheadline).foregroundStyle(Theme.textSecondary)
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 14) {
            TextField("", text: $text, prompt: Text("Paste or type…").foregroundColor(Theme.textTertiary), axis: .vertical)
                .font(.system(size: 16)).foregroundStyle(Theme.text).tint(Theme.silver)
                .lineLimit(4...12).padding(14).glass(16)
            Button {
                Haptics.tap()
                session.submit(text: text)
            } label: {
                HStack { Spacer(); Text(session.isSubmitting ? "Sending…" : "Give it to Bruce").font(.system(size: 16, weight: .semibold)); Spacer() }
                    .foregroundStyle(Theme.text).padding(15).glass(16)
            }
            .buttonStyle(PressStyle())
            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || session.isSubmitting)
            .opacity(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.5 : 1)
        }
    }

    private var workingCard: some View {
        HStack(spacing: 12) {
            ProgressView().tint(Theme.silver)
            Text(session.displayStatus.isEmpty ? "Understanding what you sent…" : session.displayStatus)
                .font(.system(size: 16, weight: .medium)).foregroundStyle(Theme.text)
            Spacer()
        }
        .padding(16).glass(16)
    }

    private var resultCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Here's what I found").font(.system(size: 18, weight: .semibold)).foregroundStyle(Theme.text)
            if let ex = session.extracted {
                if let title = ex.title, !title.isEmpty {
                    Text(title).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                }
                ForEach(ex.deadlines) { d in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: d.isAmbiguous ? "questionmark.circle" : "calendar")
                            .foregroundStyle(d.isAmbiguous ? AnyShapeStyle(Theme.textTertiary) : AnyShapeStyle(Theme.silver))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(d.label).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
                            Text(d.isAmbiguous ? "Date unclear — needs your eye" : (d.date ?? ""))
                                .font(.system(size: 13)).foregroundStyle(Theme.textSecondary)
                        }
                        Spacer()
                    }
                    .padding(12).glass(14)
                }
                if !ex.required_items.isEmpty {
                    Text("You'll also need: " + ex.required_items.map(\.name).joined(separator: ", "))
                        .font(.system(size: 13)).foregroundStyle(Theme.textSecondary)
                }
            }
            Button { Haptics.tap(); dismiss() } label: {
                HStack { Spacer(); Text("Looks right").font(.system(size: 16, weight: .semibold)); Spacer() }
                    .foregroundStyle(Theme.text).padding(15).glass(16)
            }.buttonStyle(PressStyle())
        }
    }

    private var problemCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: session.stage == .blocked ? "clock.arrow.circlepath" : "exclamationmark.triangle")
                    .foregroundStyle(Theme.textSecondary)
                Text(session.stage == .blocked ? "Hit a snag — you can retry" : "Couldn't read that one")
                    .font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
            }
            if let reason = session.blockingReason {
                Text(reason).font(.system(size: 13)).foregroundStyle(Theme.textSecondary)
            }
            Button { Haptics.tap(); session.retry() } label: {
                HStack { Spacer(); Text("Try again").font(.system(size: 16, weight: .semibold)); Spacer() }
                    .foregroundStyle(Theme.text).padding(15).glass(16)
            }.buttonStyle(PressStyle())
        }
    }
}
