import SwiftUI

/// Canonical detail for a live intake mission. Bound to the real server state via LiveIntakeSession —
/// it polls, shows grounded dates/tasks when ready, surfaces an unguessable date as a real Decision,
/// and NEVER shows "done" before the server has verified it. Reuses the app's modules + graphite/glass
/// design; adds no new palette or gradients.
struct LiveMissionDetailView: View {
    @State private var session: LiveIntakeSession
    @Environment(\.dismiss) private var dismiss

    /// From the capture sheet: reuse the session that is already polling.
    init(session: LiveIntakeSession) { _session = State(initialValue: session) }

    /// From an app relaunch: rebuild a session and re-poll the persisted mission.
    init(restoring missionID: UUID) {
        let s = LiveIntakeSession()
        s.resume(missionID: missionID)
        _session = State(initialValue: s)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                header
                switch session.stage {
                case .idle, .submitting, .working: workingModule
                case .ready:                        readyModules
                case .blocked:                      problemModule(blocked: true)
                case .failed:                       problemModule(blocked: false)
                case .sessionExpired:               expiredModule
                }
                Color.clear.frame(height: 30)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .onDisappear { session.cancel() }
    }

    private var title: String {
        if session.stage == .ready, let t = session.extracted?.title, !t.isEmpty { return t }
        return "New from you"
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                .accessibilityAddTraits(.isHeader)
            Text(stateSentence).font(.system(size: 17)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var stateSentence: String {
        switch session.stage {
        case .idle, .submitting, .working: return session.displayStatus.isEmpty ? "Understanding what you sent…" : session.displayStatus
        case .ready: return "Here's what Bruce found. Nothing is added until you say so."
        case .blocked: return "Bruce hit a snag and is retrying."
        case .failed: return "Bruce couldn't read that one."
        case .sessionExpired: return "Your session expired."
        }
    }

    // Working — honest progress, NEVER a completed state or a fake percentage.
    private var workingModule: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "Now")
            HStack(spacing: 12) {
                ProgressView().tint(Theme.text)
                Text(session.displayStatus.isEmpty ? "Understanding what you sent…" : session.displayStatus)
                    .font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
                Spacer()
            }
            .frame(maxWidth: .infinity, alignment: .leading).padding(18).glass(18)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Bruce is reading it. \(session.displayStatus)")
        }
    }

    @ViewBuilder private var readyModules: some View {
        // Ambiguity FIRST — it's the one thing that needs the student (a real Decision).
        if !session.ambiguities.isEmpty {
            Module(label: "Needs you") {
                VStack(alignment: .leading, spacing: 12) {
                    Label("Bruce won't guess a date it wasn't given", systemImage: "questionmark.circle.fill")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.amber)
                    ForEach(session.ambiguities) { d in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(d.label).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                            Text("It only said \u{201C}\(d.source_span)\u{201D} — set the real date so it's tracked.")
                                .font(.caption).foregroundStyle(Theme.textSecondary)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("Needs a date: \(d.label). Source said \(d.source_span).")
                    }
                }
            }
        }
        // Grounded deadlines — verified against the source.
        if !session.groundedDeadlines.isEmpty {
            Module(label: "Deadlines") {
                VStack(spacing: 14) {
                    ForEach(session.groundedDeadlines) { d in
                        HStack(spacing: 12) {
                            Image(systemName: "calendar").font(.system(size: 18)).foregroundStyle(Theme.silver)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(d.label).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
                                Text(d.date ?? "").font(.caption).foregroundStyle(Theme.textSecondary)
                            }
                            Spacer()
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("\(d.label), \(d.date ?? "")")
                    }
                }
            }
        }
        if let items = session.extracted?.required_items, !items.isEmpty {
            Module(label: "You'll also need") {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(items) { it in
                        HStack(spacing: 10) {
                            Image(systemName: "doc").font(.system(size: 15)).foregroundStyle(Theme.textTertiary)
                            Text(it.name).font(.system(size: 15)).foregroundStyle(Theme.text)
                            Spacer()
                        }
                    }
                }
            }
        }
        SilverButton(title: "Looks right", icon: "checkmark") { Haptics.tap(); dismiss() }
            .accessibilityHint("Confirms what Bruce found and returns home.")
    }

    private func problemModule(blocked: Bool) -> some View {
        Module(label: blocked ? "Retrying" : "Couldn't read it") {
            VStack(alignment: .leading, spacing: 12) {
                Label(blocked ? "Hit a snag — Bruce is retrying" : "Bruce couldn't read that one",
                      systemImage: blocked ? "clock.arrow.circlepath" : "exclamationmark.triangle")
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                if let r = session.blockingReason {
                    Text(r).font(.caption).foregroundStyle(Theme.textSecondary)
                }
                SilverButton(title: "Try again", icon: "arrow.clockwise") { Haptics.tap(); session.retry() }
                    .accessibilityHint("Retries reading what you sent.")
            }
        }
    }

    private var expiredModule: some View {
        Module(label: "Session expired") {
            VStack(alignment: .leading, spacing: 12) {
                Text("Your session expired. Sign in again to keep going — what you sent is safe.")
                    .font(.system(size: 15)).foregroundStyle(Theme.text)
                SilverButton(title: "Sign in again", icon: "person.crop.circle") { Haptics.tap(); dismiss() }
            }
        }
    }
}
