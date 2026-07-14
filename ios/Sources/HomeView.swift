import SwiftUI

@MainActor
final class HomeModel: ObservableObject {
    @Published var missions: [MissionSummary] = []
    @Published var topic = ""
    @Published var online = false
    @Published var creating = false

    private let api = BruceAPI()

    func refresh() async {
        online = await api.health()
        missions = (try? await api.listMissions()) ?? missions
    }

    func handoff() async {
        let t = topic.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        creating = true
        defer { creating = false }
        _ = try? await api.createResearchMission(topic: t)
        topic = ""
        await refresh()
    }
}

struct HomeView: View {
    @StateObject private var model = HomeModel()

    var body: some View {
        NavigationStack {
            List {
                Section {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("What should Bruce handle?")
                            .font(.title3.weight(.semibold))
                        TextField("Find me research in polariton chemistry…", text: $model.topic, axis: .vertical)
                            .lineLimit(1...3)
                            .padding(12)
                            .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                        Button {
                            Task { await model.handoff() }
                        } label: {
                            HStack(spacing: 6) {
                                if model.creating { ProgressView().controlSize(.small) }
                                Text(model.creating ? "Handing off…" : "Hand it to Bruce")
                                    .fontWeight(.semibold)
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                        .disabled(model.topic.trimmingCharacters(in: .whitespaces).isEmpty || model.creating)
                    }
                    .padding(.vertical, 4)
                    .listRowSeparator(.hidden)
                }

                Section("Missions") {
                    if model.missions.isEmpty {
                        Text("No missions yet — hand Bruce something above.")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(model.missions) { m in
                            HStack(spacing: 12) {
                                Image(systemName: icon(for: m.phase))
                                    .foregroundStyle(.tint)
                                    .frame(width: 22)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(m.short_status).font(.body.weight(.medium))
                                    Text(pretty(m.phase))
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            .padding(.vertical, 2)
                        }
                    }
                }
            }
            .navigationTitle("Bruce")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    HStack(spacing: 6) {
                        Circle().fill(model.online ? .green : .secondary).frame(width: 9, height: 9)
                        Text(model.online ? "Connected" : "Offline")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
            .refreshable { await model.refresh() }
            .task { await model.refresh() }
        }
    }

    private func pretty(_ phase: String) -> String {
        phase.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func icon(for phase: String) -> String {
        switch phase {
        case "succeeded": return "checkmark.seal.fill"
        case "failed": return "exclamationmark.triangle.fill"
        case "awaiting_approval": return "hand.raised.fill"
        case "verifying": return "checklist"
        default: return "circle.dashed"
        }
    }
}
