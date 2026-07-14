import SwiftUI

/// Shared scaffold for the settings sub-pages.
struct SettingsScaffold<C: View>: View {
    let title: String
    let subtitle: String
    @ViewBuilder var content: C
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(title).font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text(subtitle).font(.subheadline).foregroundStyle(Theme.textSecondary)
                }
                content
                Color.clear.frame(height: 30)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .hidesTabBar()
    }
}

private func toggleRow(_ t: String, _ b: Binding<Bool>) -> some View {
    Toggle(isOn: b) { Text(t).font(.system(size: 15)).foregroundStyle(Theme.text) }.tint(Theme.green)
}

// MARK: - Notifications

struct NotificationsSettingsView: View {
    @State private var deadlineRisk = true
    @State private var decisions = true
    @State private var verified = true
    @State private var changes = true
    @State private var quiet = false
    var body: some View {
        SettingsScaffold(title: "Notifications", subtitle: "Bruce only notifies you when it actually matters.") {
            Module(label: "Notify me about") {
                VStack(spacing: 16) {
                    toggleRow("A deadline is at risk", $deadlineRisk)
                    toggleRow("A decision needs me", $decisions)
                    toggleRow("Something is verified complete", $verified)
                    toggleRow("Important information changes", $changes)
                }
            }
            Module(label: "Quiet hours") {
                toggleRow("Mute 10:00 PM – 7:00 AM", $quiet)
            }
        }
    }
}

// MARK: - Privacy

struct PrivacyView: View {
    @State private var policy = 0
    private let policies = ["When a mission completes", "After 7 days", "After 30 days"]
    var body: some View {
        SettingsScaffold(title: "Privacy", subtitle: "You control what Bruce keeps and for how long.") {
            Module(label: "What Bruce stores") {
                VStack(alignment: .leading, spacing: 12) {
                    storeRow("Missions and drafts", "Kept until you delete them")
                    storeRow("Forwarded content", "Auto-deletes on the schedule below")
                    storeRow("Profile and settings", "Kept until you delete your account")
                }
            }
            Module(label: "Auto-delete forwarded content") {
                VStack(spacing: 10) {
                    ForEach(Array(policies.enumerated()), id: \.offset) { i, p in
                        Button { Haptics.select(); policy = i } label: {
                            HStack {
                                Text(p).font(.system(size: 15)).foregroundStyle(Theme.text)
                                Spacer()
                                Image(systemName: policy == i ? "largecircle.fill.circle" : "circle")
                                    .font(.system(size: 19)).foregroundStyle(policy == i ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.textTertiary))
                            }
                        }.buttonStyle(PressStyle())
                    }
                }
            }
            Button { Haptics.tap() } label: {
                HStack { Image(systemName: "square.and.arrow.up"); Text("Export everything").font(.system(size: 16, weight: .semibold)); Spacer() }
                    .foregroundStyle(Theme.text).padding(16).glass(16)
            }.buttonStyle(PressStyle())
        }
    }
    private func storeRow(_ t: String, _ s: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(t).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
            Text(s).font(.caption).foregroundStyle(Theme.textTertiary)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Communication style

struct CommunicationStyleView: View {
    @State private var style = 0
    private let styles: [(String, String)] = [
        ("Concise", "Short and direct. Just the essentials."),
        ("Warm", "Friendly and encouraging, still to the point."),
        ("Formal", "Polished and professional for official messages."),
    ]
    var body: some View {
        SettingsScaffold(title: "Communication style", subtitle: "How Bruce writes on your behalf. You always approve messages first.") {
            VStack(spacing: 12) {
                ForEach(Array(styles.enumerated()), id: \.offset) { i, s in
                    Button { Haptics.select(); style = i } label: {
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: style == i ? "largecircle.fill.circle" : "circle")
                                .font(.system(size: 20)).foregroundStyle(style == i ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.textTertiary))
                            VStack(alignment: .leading, spacing: 3) {
                                Text(s.0).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                                Text(s.1).font(.subheadline).foregroundStyle(Theme.textSecondary)
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(16).glass(18)
                    }.buttonStyle(PressStyle())
                }
            }
        }
    }
}

// MARK: - Personal protocols

struct PersonalProtocolsView: View {
    private let rules = [
        "Never send emails after 9:00 PM",
        "CC my counselor on official emails",
        "Prefer opportunities with no essay",
        "Always ask before anything costs money",
    ]
    var body: some View {
        SettingsScaffold(title: "Personal protocols", subtitle: "Standing rules Bruce follows on every mission.") {
            VStack(spacing: 12) {
                ForEach(rules, id: \.self) { r in
                    HStack(spacing: 12) {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green)
                        Text(r).font(.system(size: 15)).foregroundStyle(Theme.text)
                        Spacer()
                    }.padding(14).glass(16)
                }
                Button { Haptics.tap() } label: {
                    HStack { Image(systemName: "plus"); Text("Add a protocol").font(.system(size: 15, weight: .semibold)); Spacer() }
                        .foregroundStyle(Theme.silver).padding(14).glass(16)
                }.buttonStyle(PressStyle())
            }
        }
    }
}

// MARK: - Help / Report / About

struct HelpView: View {
    private let faqs = [
        "How does Bruce avoid making things up?",
        "What does Bruce do automatically?",
        "How do I connect my school calendar?",
        "How do I delete my data?",
    ]
    var body: some View {
        SettingsScaffold(title: "Help", subtitle: "Answers to the common questions.") {
            VStack(spacing: 12) {
                ForEach(faqs, id: \.self) { q in
                    HStack {
                        Text(q).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
                        Spacer(minLength: 8)
                        Image(systemName: "chevron.right").font(.footnote.weight(.bold)).foregroundStyle(Theme.textTertiary)
                    }.padding(16).glass(16)
                }
            }
        }
    }
}

struct ReportProblemView: View {
    @State private var attachDiagnostics = false
    var body: some View {
        SettingsScaffold(title: "Report a problem", subtitle: "Tell us what went wrong and we'll look into it.") {
            VStack(alignment: .leading, spacing: 8) {
                Text("What happened?").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.textSecondary)
                Text("Describe the issue…").font(.system(size: 15)).foregroundStyle(Theme.textTertiary)
                    .frame(maxWidth: .infinity, minHeight: 120, alignment: .topLeading).padding(14).glass(16)
            }
            toggleRow("Attach diagnostics (no personal content)", $attachDiagnostics)
            SilverButton(title: "Send report", icon: "paperplane.fill") {}
        }
    }
}

struct AboutView: View {
    var body: some View {
        SettingsScaffold(title: "About Bruce", subtitle: "The operating system for student life.") {
            Module(label: "Version") {
                HStack { Text("Bruce").font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text); Spacer(); Text("0.1.0 (1)").font(.subheadline).foregroundStyle(Theme.textSecondary) }
            }
            Module(label: "Our promise") {
                Text("Every email and fact traces to a real source. Bruce won't invent a person, a paper, or an address — and nothing leaves without your ok.")
                    .font(.subheadline).foregroundStyle(Theme.textSecondary)
            }
            VStack(spacing: 12) {
                aboutLink("Privacy policy"); aboutLink("Terms of service")
            }
        }
    }
    private func aboutLink(_ t: String) -> some View {
        Button { Haptics.tap() } label: {
            HStack { Text(t).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text); Spacer()
                Image(systemName: "arrow.up.right").font(.caption.weight(.bold)).foregroundStyle(Theme.textTertiary) }
                .padding(16).glass(16)
        }.buttonStyle(PressStyle())
    }
}
