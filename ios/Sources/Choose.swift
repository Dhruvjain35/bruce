import SwiftUI

struct ChooseRecommenderView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var selected: UUID?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Pick a recommender").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text("The summer program needs one teacher rec. Bruce will draft the ask for your approval.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }
                VStack(spacing: 12) {
                    ForEach(Mock.recommenders) { r in
                        let on = selected == r.id
                        Button { Haptics.select(); selected = r.id } label: {
                            HStack(spacing: 12) {
                                Text(String(r.name.prefix(1))).font(.system(size: 18, weight: .bold, design: .rounded))
                                    .foregroundStyle(Theme.bg).frame(width: 46, height: 46).background(Theme.silver, in: Circle())
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(r.name).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                                    Text(r.subject).font(.caption).foregroundStyle(Theme.textTertiary)
                                    Text(r.note).font(.subheadline).foregroundStyle(Theme.textSecondary)
                                }
                                Spacer()
                                Image(systemName: on ? "largecircle.fill.circle" : "circle")
                                    .font(.system(size: 20)).foregroundStyle(on ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.textTertiary))
                            }
                            .padding(16).glass(18)
                            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
                                .strokeBorder(on ? AnyShapeStyle(Theme.silver.opacity(0.5)) : AnyShapeStyle(Color.clear), lineWidth: 1))
                        }.buttonStyle(PressStyle())
                    }
                }
                Color.clear.frame(height: 90)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .hidesTabBar()
        .safeAreaInset(edge: .bottom) {
            SilverButton(title: selectedName.map { "Draft the ask to \($0)" } ?? "Select a teacher", icon: "envelope") {
                if selected != nil { dismiss() }
            }
            .opacity(selected == nil ? 0.5 : 1)
            .padding(.horizontal, 20).padding(.bottom, 10).background(Theme.bottomFade)
        }
    }

    private var selectedName: String? {
        Mock.recommenders.first { $0.id == selected }?.name.components(separatedBy: " ").last
    }
}

/// Review the dates Bruce found before any are added to the calendar.
struct DatesReviewView: View {
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Review dates").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text("Bruce found these. Nothing is added until you say so.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }
                VStack(spacing: 12) {
                    ForEach(Mock.calendar) { c in
                        HStack(spacing: 12) {
                            VStack(spacing: 2) {
                                Text(c.mon).font(.caption2.weight(.bold)).foregroundStyle(Theme.textSecondary)
                                Text(c.num).font(.system(size: 18, weight: .bold, design: .rounded)).foregroundStyle(Theme.silver)
                            }.frame(width: 48).padding(.vertical, 6)
                            .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                            VStack(alignment: .leading, spacing: 2) {
                                Text(c.title).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text).lineLimit(1)
                                Text(c.time).font(.caption).foregroundStyle(Theme.textSecondary)
                            }
                            Spacer(minLength: 6)
                            if c.state == .added {
                                Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green)
                            } else {
                                Button { Haptics.tap() } label: {
                                    Text("Add").font(.system(size: 13, weight: .bold)).foregroundStyle(Theme.bg)
                                        .padding(.vertical, 7).padding(.horizontal, 14).background(Theme.silver, in: Capsule())
                                }.buttonStyle(PressStyle())
                            }
                        }
                        .padding(14).glass(16)
                    }
                }
                Color.clear.frame(height: 90)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .hidesTabBar()
        .safeAreaInset(edge: .bottom) {
            SilverButton(title: "Add the 2 conflict-free dates", icon: "calendar.badge.plus") { dismiss() }
                .padding(.horizontal, 20).padding(.bottom, 10).background(Theme.bottomFade)
        }
    }
}
