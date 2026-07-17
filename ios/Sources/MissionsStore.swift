import SwiftUI
import Observation

/// Home's real mission data. Fetches GET /v1/missions and exposes explicit UI states — no mock data
/// on the shipping path, and never a fabricated mission. Buckets by phase for the Home sections.
@Observable final class MissionsStore {
    enum Kind: Equatable { case offline, expired, server }
    enum State: Equatable { case loading, loaded, empty, error(Kind) }

    var state: State = .loading
    private(set) var missions: [MissionSummary] = []

    private var api: BruceAPI

    init(api: BruceAPI = BruceAPI()) { self.api = api }

    @MainActor func refresh() async {
        // No credential -> a signed-out/expired state, not a spurious network error.
        guard AppSession.shared.bearer != nil else { state = .error(.expired); return }
        if missions.isEmpty { state = .loading }
        do {
            let list = try await api.listMissions()
            missions = list
            state = list.isEmpty ? .empty : .loaded
        } catch let BruceAPIError.badStatus(code) {
            Reporter.report(area: "home", code: "http_\(code)")
            state = .error(code == 401 ? .expired : .server)
        } catch {
            Reporter.report(area: "home", code: "network")
            state = .error(.offline)
        }
    }

    // Phase buckets (match the engine MissionPhase values).
    var needsYou: [MissionSummary] { missions.filter { ["awaiting_approval", "failed", "blocked"].contains($0.phase) } }
    var working: [MissionSummary] {
        missions.filter { ["created", "understanding", "extracting", "running", "executing", "waiting_external", "verifying"].contains($0.phase) }
    }
    var done: [MissionSummary] { missions.filter { $0.phase == "succeeded" } }
}

/// A real-mission row for Home — same glass language as HomeMissionRow, driven by canonical state.
struct LiveMissionRow: View {
    let m: MissionSummary
    private var isFailed: Bool { m.phase == "failed" }
    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                Text(m.short_status).font(.subheadline)
                    .foregroundStyle(isFailed ? Theme.red : Theme.textSecondary).lineLimit(1)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.footnote.weight(.bold)).foregroundStyle(Theme.textTertiary)
        }
        .padding(.vertical, 15).padding(.horizontal, 16).glass(16)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title). \(m.short_status)")
    }
    private var title: String {
        switch m.phase {
        case "awaiting_approval": return "Ready to review"
        case "failed": return "Couldn't read it"
        case "blocked": return "Retrying"
        case "succeeded": return "Done"
        default: return "Working on it"
        }
    }
}
