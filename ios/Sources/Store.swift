import SwiftUI
import Observation

/// Single source of truth for the mock. Mutating it updates every surface that reads it —
/// approving a decision moves its mission, clears the decision, and drops the tab badge.
@Observable final class BruceStore {
    var missions: [Mission] = Mock.missions
    var decisions: [Decision] = Mock.decisions
    var automationMode: AutomationMode = .smartAuto
    var autoPaused = false

    var needsYou: [Mission] { missions.filter { $0.status == .needsYou || $0.status == .failed } }
    var working: [Mission] { missions.filter { $0.status == .working } }
    var activeCount: Int { missions.count }

    func mission(_ id: UUID) -> Mission? { missions.first { $0.id == id } }

    /// Approve the outreach email → the mission starts sending and its decision clears.
    func approveEmail() {
        if let i = missions.firstIndex(where: { $0.draft != nil }) {
            missions[i].status = .working
            missions[i].statusText = "Sending…"
            missions[i].homeLine = "Sending to Prof. Huo…"
            missions[i].now = "Sending the email"
        }
        decisions.removeAll { $0.cta == "Review email" }
    }

    func pauseMission(_ id: UUID) {
        guard let i = missions.firstIndex(where: { $0.id == id }) else { return }
        missions[i].now = "Paused"
        missions[i].statusText = "Paused"
        missions[i].homeLine = "Paused by you"
    }

    func cancelMission(_ id: UUID) { missions.removeAll { $0.id == id } }

    func retryMission(_ id: UUID) {
        guard let i = missions.firstIndex(where: { $0.id == id }) else { return }
        missions[i].status = .working
        missions[i].statusText = "Retrying the upload…"
        missions[i].homeLine = "Retrying the upload…"
        missions[i].now = "Retrying the upload"
    }
}
