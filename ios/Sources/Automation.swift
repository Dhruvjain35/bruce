import SwiftUI

struct AutomationView: View {
    @Environment(BruceStore.self) private var store

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Automation").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text("How much should Bruce handle automatically? You can change this anytime.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }

                VStack(spacing: 12) {
                    ForEach(AutomationMode.allCases) { mode in modeCard(mode) }
                }

                Module(label: "Bruce can act automatically") {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(Mock.canAuto, id: \.self) { c in
                            HStack(spacing: 10) {
                                Image(systemName: "checkmark").font(.system(size: 12, weight: .bold)).foregroundStyle(Theme.green)
                                Text(c).font(.system(size: 15)).foregroundStyle(Theme.textSecondary)
                            }
                        }
                    }
                }

                Module(label: "Bruce always asks") {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(Mock.alwaysAsk, id: \.self) { a in
                            HStack(spacing: 10) {
                                Image(systemName: "lock.fill").font(.system(size: 12, weight: .bold)).foregroundStyle(Theme.amber)
                                Text(a).font(.system(size: 15)).foregroundStyle(Theme.textSecondary)
                            }
                        }
                    }
                }

                Module(label: "Recent automatic actions") {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(Mock.recentAuto) { a in
                            HStack {
                                Text(a.title).font(.system(size: 15)).foregroundStyle(Theme.text)
                                Spacer()
                                Text(a.when).font(.caption).foregroundStyle(Theme.textTertiary)
                            }
                        }
                    }
                }

                pauseControl
                Color.clear.frame(height: 30)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .hidesTabBar()
    }

    private func modeCard(_ mode: AutomationMode) -> some View {
        let on = store.automationMode == mode
        return Button {
            Haptics.select(); store.automationMode = mode
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: on ? "largecircle.fill.circle" : "circle")
                    .font(.system(size: 20)).foregroundStyle(on ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.textTertiary))
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text(mode.rawValue).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                        if mode == .smartAuto {
                            Text("Recommended").font(.caption2.weight(.bold)).foregroundStyle(Theme.bg)
                                .padding(.horizontal, 7).padding(.vertical, 2).background(Theme.silver, in: Capsule())
                        }
                    }
                    Text(mode.blurb).font(.subheadline).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(16)
            .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
                .strokeBorder(on ? AnyShapeStyle(Theme.silver.opacity(0.5)) : AnyShapeStyle(Theme.silverEdge), lineWidth: 1))
        }.buttonStyle(PressStyle())
    }

    private var pauseControl: some View {
        Button {
            Haptics.tap(); store.autoPaused.toggle()
        } label: {
            HStack(spacing: 10) {
                Image(systemName: store.autoPaused ? "play.fill" : "pause.fill")
                Text(store.autoPaused ? "Resume automatic actions" : "Pause all automatic actions")
                    .font(.system(size: 16, weight: .bold))
                Spacer()
            }
            .foregroundStyle(store.autoPaused ? Theme.green : Theme.red)
            .padding(16)
            .background((store.autoPaused ? Theme.green : Theme.red).opacity(0.08), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder((store.autoPaused ? Theme.green : Theme.red).opacity(0.25)))
        }.buttonStyle(PressStyle())
    }
}
