import SwiftUI

struct HomeView: View {
    var onSelectTab: (Int) -> Void = { _ in }
    @State private var showHandoff = false
    @State private var autoDetail = false

    var body: some View {
      NavigationStack {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                header
                Button { showHandoff = true } label: { HandoffBar() }.buttonStyle(PressStyle())

                section("Today") { today }

                if !Mock.needsYou.isEmpty {
                    section("Needs you") {
                        VStack(spacing: 10) {
                            ForEach(Mock.needsYou) { m in
                                NavigationLink { MissionDetailView(m: m) } label: { HomeMissionRow(m: m) }
                                    .buttonStyle(PressStyle())
                            }
                        }
                    }
                }

                section("Coming up") { comingUp }

                if !Mock.working.isEmpty {
                    section("Working") {
                        VStack(spacing: 10) {
                            ForEach(Mock.working) { m in
                                NavigationLink { MissionDetailView(m: m) } label: { HomeMissionRow(m: m) }
                                    .buttonStyle(PressStyle())
                            }
                        }
                    }
                }

                Color.clear.frame(height: 96)
            }
            .padding(.horizontal, 20)
            .padding(.top, 6)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .safeAreaInset(edge: .top) { if Demo.state == "offline" { OfflineBanner() } }
        .overlay(alignment: .bottom) {
            if Demo.state == "undo" {
                Toast(text: "Sent to Prof. Huo", action: "Undo").padding(.bottom, 108)
            }
        }
        .toolbar(.hidden, for: .navigationBar)
        .navigationDestination(isPresented: $autoDetail) {
            MissionDetailView(m: Demo.present == "failure" ? Mock.failureMission : Mock.missions[0])
        }
        .sheet(isPresented: $showHandoff) { HandoffSheet() }
        .onAppear {
            switch Demo.present {
            case "handoff", "clarify": showHandoff = true
            case "detail", "approval", "failure": autoDetail = true
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

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 1) {
                Text(Mock.greeting).font(.system(size: 17)).foregroundStyle(Theme.textSecondary)
                Text(Mock.studentName).font(.system(size: 30, weight: .bold)).foregroundStyle(Theme.text)
            }
            Spacer()
            Button { onSelectTab(4) } label: {
                Image(systemName: "bell")
                    .font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                    .frame(width: 42, height: 42)
                    .glass(21)
            }.buttonStyle(PressStyle())
        }
    }

    private var today: some View {
        HStack(spacing: 10) {
            Button { onSelectTab(2) } label: { TodayChip(n: Mock.todayDeadlines, label: Mock.todayDeadlines == 1 ? "deadline" : "deadlines") }
                .buttonStyle(PressStyle())
            Button { onSelectTab(3) } label: { TodayChip(n: Mock.todayDecisions, label: Mock.todayDecisions == 1 ? "decision" : "decisions") }
                .buttonStyle(PressStyle())
            Button { onSelectTab(1) } label: { TodayChip(n: Mock.activeMissions, label: "active") }
                .buttonStyle(PressStyle())
            Spacer(minLength: 0)
        }
    }

    private var comingUp: some View {
        VStack(spacing: 0) {
            ForEach(Array(Mock.comingUp.enumerated()), id: \.element.id) { i, c in
                HStack {
                    Text(c.title).font(.subheadline.weight(.medium)).foregroundStyle(Theme.text)
                    Spacer()
                    Text(c.when).font(.subheadline).foregroundStyle(Theme.textSecondary)
                }
                .padding(.vertical, 13).padding(.horizontal, 16)
                if i < Mock.comingUp.count - 1 { Divider().overlay(Theme.stroke).padding(.leading, 16) }
            }
        }
        .glass(16)
    }
}
