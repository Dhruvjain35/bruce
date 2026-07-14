import SwiftUI

/// Lightweight shared UI state (e.g. hide the floating tab bar on pushed detail screens).
final class AppState: ObservableObject {
    @Published var hideTabBar = false
}

struct RootView: View {
    @StateObject private var app = AppState()
    @State private var tab = Int(ProcessInfo.processInfo.environment["BRUCE_TAB"] ?? "") ?? 0
    private let tabs: [(icon: String, label: String)] = [
        ("house.fill", "Home"),
        ("flag.fill", "Missions"),
        ("calendar", "Calendar"),
        ("checklist", "Decisions"),
        ("person.fill", "You"),
    ]

    var body: some View {
        ZStack(alignment: .bottom) {
            Theme.Backdrop()
            Group {
                switch tab {
                case 0: HomeView(onSelectTab: { i in withAnimation(.easeInOut(duration: 0.2)) { tab = i } })
                case 1: MissionsView()
                case 2: CalendarView()
                case 3: DecisionsView()
                default: YouView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            if !app.hideTabBar {
                tabBar.transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .environmentObject(app)
        .animation(.easeInOut(duration: 0.25), value: app.hideTabBar)
        .preferredColorScheme(.dark)
    }

    private var tabBar: some View {
        HStack(spacing: 0) {
            ForEach(Array(tabs.enumerated()), id: \.offset) { i, t in
                Button {
                    Haptics.select()
                    withAnimation(.easeInOut(duration: 0.2)) { tab = i }
                } label: {
                    VStack(spacing: 4) {
                        Image(systemName: t.icon).font(.system(size: 17, weight: .semibold))
                            .overlay(alignment: .topTrailing) {
                                if i == 3 && !Mock.decisions.isEmpty {
                                    Circle().fill(Theme.amber).frame(width: 7, height: 7).offset(x: 4, y: -2)
                                }
                            }
                        Text(t.label).font(.system(size: 10, weight: .semibold))
                    }
                    .foregroundStyle(tab == i ? Theme.text : Theme.textTertiary)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 10).padding(.bottom, 8)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous).strokeBorder(Theme.strokeHi, lineWidth: 1))
        .padding(.horizontal, 20)
        .padding(.bottom, 4)
    }
}

struct ComingSoon: View {
    let title: String
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: "hammer.fill").font(.system(size: 34)).foregroundStyle(Theme.silver)
            Text(title).font(.title2.weight(.bold)).foregroundStyle(Theme.text)
            Text("Designing this screen next.").foregroundStyle(Theme.textSecondary)
        }
    }
}
