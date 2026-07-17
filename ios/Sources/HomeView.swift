import SwiftUI

struct HomeView: View {
    var onSelectTab: (Int) -> Void = { _ in }
    @State private var showHandoff = false
    @State private var autoDetail = false
    @State private var liveSession: LiveIntakeSession? = nil
    @State private var goLive = false
    @State private var restoreID: UUID? = nil
    @State private var didCheckRestore = false
    @State private var missions = MissionsStore()

    var body: some View {
      NavigationStack {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                header
                Button { showHandoff = true } label: { HandoffBar() }.buttonStyle(PressStyle())
                missionContent
                Color.clear.frame(height: 96)
            }
            .padding(.horizontal, 20)
            .padding(.top, 6)
        }
        .scrollIndicators(.hidden)
        .refreshable { await missions.refresh() }
        .task { await missions.refresh() }   // load on appear; restores the list after relaunch
        .background(Theme.Backdrop())
        .safeAreaInset(edge: .top) { if Demo.state == "offline" { OfflineBanner() } }
        .overlay(alignment: .bottom) {
            if Demo.state == "undo" {
                // Undo is only for reversible actions — never for a sent email.
                Toast(text: "Added Science Fair deadline", action: "Undo").padding(.bottom, 108)
            }
        }
        .toolbar(.hidden, for: .navigationBar)
        .navigationDestination(isPresented: $autoDetail) {
            MissionDetailView(mission: Demo.present == "failure" ? Mock.failureMission : Mock.missions[0])
        }
        .sheet(isPresented: $showHandoff) {
            HandoffSheet { session in
                // Mission durably acknowledged — hand off to the canonical detail (it keeps polling).
                liveSession = session
                goLive = true
                Task { await missions.refresh() }   // reflect the new mission when we return Home
            }
        }
        .navigationDestination(isPresented: $goLive) {
            if let s = liveSession { LiveMissionDetailView(session: s) }
        }
        .navigationDestination(
            isPresented: Binding(get: { restoreID != nil }, set: { if !$0 { restoreID = nil } })
        ) {
            if let id = restoreID { LiveMissionDetailView(restoring: id) }
        }
        .onAppear {
            // Restore an in-flight intake mission after an app relaunch (id only was persisted).
            if !didCheckRestore { didCheckRestore = true; restoreID = IntakeRestore.pending }
        }
        .onAppear {
            switch Demo.present {
            case "handoff", "clarify": showHandoff = true
            case "detail", "approval", "failure", "person", "editplan": autoDetail = true
            default: break
            }
        }
      }
    }

    private func section<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionLabel(text: title)
            content()
        }
    }

    // MARK: - Real missions (GET /v1/missions) with explicit states

    @ViewBuilder private var missionContent: some View {
        switch missions.state {
        case .loading:
            VStack(spacing: 10) { ForEach(0..<2, id: \.self) { _ in MissionSkeleton() } }
        case .empty:
            emptyState
        case .error(let kind):
            errorState(kind)
        case .loaded:
            if !missions.needsYou.isEmpty { missionSection("Needs you", missions.needsYou) }
            if !missions.working.isEmpty { missionSection("Working", missions.working) }
            if !missions.done.isEmpty { missionSection("Done", missions.done) }
        }
    }

    private func missionSection(_ title: String, _ items: [MissionSummary]) -> some View {
        section(title) {
            VStack(spacing: 10) {
                ForEach(items) { m in
                    NavigationLink { LiveMissionDetailView(restoring: m.mission_id) } label: { LiveMissionRow(m: m) }
                        .buttonStyle(PressStyle())
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "tray").font(.system(size: 30)).foregroundStyle(Theme.textTertiary)
            Text("Nothing yet").font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
            Text("Hand Bruce a flyer, screenshot, PDF, or a note to get started.")
                .font(.subheadline).foregroundStyle(Theme.textSecondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity).padding(.vertical, 44)
        .accessibilityElement(children: .combine).accessibilityLabel("Nothing yet. Hand Bruce something to get started.")
    }

    @ViewBuilder private func errorState(_ kind: MissionsStore.Kind) -> some View {
        VStack(spacing: 12) {
            Image(systemName: kind == .offline ? "wifi.exclamationmark" : "exclamationmark.triangle")
                .font(.system(size: 26)).foregroundStyle(Theme.textSecondary)
            Text(kind == .expired ? "Your session expired" : kind == .offline ? "You're offline" : "Couldn't load your missions")
                .font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
            if kind == .expired {
                SilverButton(title: "Sign in again", icon: "person.crop.circle") { AppSession.shared.signOut() }
            } else {
                SilverButton(title: "Try again", icon: "arrow.clockwise") { Task { await missions.refresh() } }
            }
        }
        .frame(maxWidth: .infinity).padding(.vertical, 40)
        .accessibilityElement(children: .combine)
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 1) {
                Text(Mock.greeting).font(.system(size: 17)).foregroundStyle(Theme.textSecondary)
                Text(Mock.studentName).font(.system(size: 30, weight: .bold)).foregroundStyle(Theme.text)
            }
            Spacer()
            NavigationLink { NotificationsSettingsView() } label: {
                Image(systemName: "bell")
                    .font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                    .frame(width: 42, height: 42)
                    .glass(21)
            }.buttonStyle(PressStyle())
        }
    }

}
