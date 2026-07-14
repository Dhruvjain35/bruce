import SwiftUI

/// Editable, mission-specific plan. What it shows adapts to the mission:
/// outreach → follow-up cadence; applications → submission gating; etc.
struct EditPlanView: View {
    let mission: Mission
    @Environment(BruceStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    @State private var stepOn: [Bool]
    @State private var waitDays: Int
    @State private var maxFollow: Int
    @State private var followEnabled: Bool
    @State private var askBeforeSubmit = true

    init(mission: Mission) {
        self.mission = mission
        _stepOn = State(initialValue: mission.afterApproval.map { _ in true })
        _waitDays = State(initialValue: mission.followUp?.waitDays ?? 5)
        _maxFollow = State(initialValue: mission.followUp?.maxFollowUps ?? 1)
        _followEnabled = State(initialValue: mission.followUp?.enabled ?? true)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Edit plan").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text("How Bruce handles “\(mission.title)” after you approve.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }

                Module(label: "Steps") {
                    VStack(spacing: 14) {
                        ForEach(Array(mission.afterApproval.enumerated()), id: \.offset) { i, s in
                            Toggle(isOn: $stepOn[i]) {
                                Text(s).font(.system(size: 15)).foregroundStyle(Theme.text)
                            }
                            .tint(Theme.green)
                        }
                    }
                }

                if mission.followUp != nil {
                    Module(label: "Follow-up") {
                        VStack(alignment: .leading, spacing: 16) {
                            Toggle(isOn: $followEnabled) {
                                Text("Follow up if there's no reply").font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
                            }.tint(Theme.green)
                            if followEnabled {
                                stepper("Wait before following up", value: $waitDays, suffix: waitDays == 1 ? "day" : "days", range: 1...14)
                                stepper("Maximum follow-ups", value: $maxFollow, suffix: maxFollow == 1 ? "time" : "times", range: 1...3)
                                HStack {
                                    Text("Stop when").font(.subheadline).foregroundStyle(Theme.textSecondary)
                                    Spacer()
                                    Text("They reply").font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
                                }
                            }
                        }
                    }
                }

                if mission.count != nil {
                    Module(label: "Before submitting") {
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Label("Always ask before submitting", systemImage: "lock.fill")
                                    .font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
                                Spacer()
                                Text("On").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.amber)
                            }
                            Text("Submitting an application is irreversible, so Bruce always asks first.")
                                .font(.caption).foregroundStyle(Theme.textTertiary)
                        }
                    }
                }

                Color.clear.frame(height: 90)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            SilverButton(title: "Save plan", icon: "checkmark") { save(); dismiss() }
                .padding(.horizontal, 20).padding(.bottom, 10).background(.ultraThinMaterial)
        }
    }

    private func stepper(_ label: String, value: Binding<Int>, suffix: String, range: ClosedRange<Int>) -> some View {
        HStack {
            Text(label).font(.subheadline).foregroundStyle(Theme.textSecondary)
            Spacer()
            HStack(spacing: 14) {
                Button { Haptics.select(); if value.wrappedValue > range.lowerBound { value.wrappedValue -= 1 } } label: {
                    Image(systemName: "minus").font(.system(size: 13, weight: .bold)).foregroundStyle(Theme.text)
                        .frame(width: 30, height: 30).background(Theme.surfaceHi, in: Circle())
                }.buttonStyle(PressStyle())
                Text("\(value.wrappedValue) \(suffix)").font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                    .frame(minWidth: 64)
                Button { Haptics.select(); if value.wrappedValue < range.upperBound { value.wrappedValue += 1 } } label: {
                    Image(systemName: "plus").font(.system(size: 13, weight: .bold)).foregroundStyle(Theme.text)
                        .frame(width: 30, height: 30).background(Theme.surfaceHi, in: Circle())
                }.buttonStyle(PressStyle())
            }
        }
    }

    private func save() {
        guard let i = store.missions.firstIndex(where: { $0.id == mission.id }) else { return }
        if store.missions[i].followUp != nil {
            store.missions[i].followUp = FollowUp(
                waitDays: waitDays, maxFollowUps: maxFollow,
                stopCondition: mission.followUp?.stopCondition ?? "Stop after any reply",
                enabled: followEnabled
            )
        }
    }
}
