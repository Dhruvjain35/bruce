import SwiftUI

// MARK: - Shared small pieces

struct EvidenceRow: View {
    let e: EvidenceSource
    var body: some View {
        Button { Haptics.tap() } label: {
            HStack(spacing: 12) {
                Image(systemName: e.icon).font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.silver)
                    .frame(width: 40, height: 40)
                    .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Theme.stroke))
                VStack(alignment: .leading, spacing: 2) {
                    Text(e.kind.uppercased()).font(.system(size: 10, weight: .bold)).tracking(0.8)
                        .foregroundStyle(Theme.textTertiary)
                    Text(e.title).font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Theme.text).lineLimit(1)
                    Text(e.meta).font(.caption).foregroundStyle(Theme.textSecondary).lineLimit(1)
                }
                Spacer(minLength: 8)
                Image(systemName: "arrow.up.right").font(.caption.weight(.bold)).foregroundStyle(Theme.textTertiary)
            }
        }.buttonStyle(PressStyle())
    }
}

/// Silver primary action button.
struct SilverButton: View {
    let title: String
    var icon: String? = nil
    let action: () -> Void
    var body: some View {
        Button { Haptics.tap(); action() } label: {
            HStack(spacing: 8) {
                if let icon { Image(systemName: icon).font(.system(size: 15, weight: .bold)) }
                Text(title).font(.system(size: 16, weight: .bold))
            }
            .foregroundStyle(Theme.bg)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 15)
            .background(Theme.silver, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
        .buttonStyle(PressStyle())
    }
}

struct GhostButton: View {
    let title: String
    var icon: String? = nil
    let action: () -> Void
    var body: some View {
        Button { Haptics.tap(); action() } label: {
            HStack(spacing: 8) {
                if let icon { Image(systemName: icon).font(.system(size: 15, weight: .semibold)) }
                Text(title).font(.system(size: 16, weight: .semibold))
            }
            .foregroundStyle(Theme.text)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 15)
            .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke))
        }
        .buttonStyle(PressStyle())
    }
}

// MARK: - Mission detail

struct MissionDetailView: View {
    let m: Mission
    @State private var showApproval = false
    @EnvironmentObject private var app: AppState

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 10) {
                    Text(m.title).font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text(m.stateSentence).font(.system(size: 17)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Updated \(m.updated)").font(.caption).foregroundStyle(Theme.textTertiary)
                }

                // NOW — the dominant element.
                VStack(alignment: .leading, spacing: 8) {
                    SectionLabel(text: "Now")
                    Text(m.now).font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.text)
                        .fixedSize(horizontal: false, vertical: true)
                    if let c = m.count {
                        Text("\(c.done) of \(c.total) \(c.noun) collected")
                            .font(.subheadline.weight(.medium)).foregroundStyle(Theme.silver)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(18)
                .glass(18)

                if !m.next.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        SectionLabel(text: "Next")
                        Text(m.next).font(.system(size: 16)).foregroundStyle(Theme.textSecondary)
                    }
                }

                VStack(alignment: .leading, spacing: 10) {
                    SectionLabel(text: "Completed")
                    ForEach(m.completed, id: \.self) { step in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Image(systemName: "checkmark").font(.system(size: 12, weight: .bold)).foregroundStyle(Theme.green.opacity(0.7))
                            Text(step).font(.system(size: 15)).foregroundStyle(Theme.textTertiary)
                        }
                    }
                }

                VStack(alignment: .leading, spacing: 12) {
                    SectionLabel(text: "Grounded in")
                    VStack(spacing: 4) { ForEach(m.evidence) { EvidenceRow(e: $0) } }
                        .padding(12).glass(16)
                }

                Color.clear.frame(height: (m.draft != nil || m.status == .failed) ? 96 : 40)
            }
            .padding(.horizontal, 20)
            .padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button { } label: { Label("Pause mission", systemImage: "pause.circle") }
                    Button(role: .destructive) { } label: { Label("Cancel mission", systemImage: "xmark.circle") }
                } label: {
                    Image(systemName: "ellipsis").font(.system(size: 16, weight: .bold)).foregroundStyle(Theme.text)
                }
            }
        }
        .safeAreaInset(edge: .bottom) {
            if m.draft != nil {
                SilverButton(title: "Review email", icon: "hand.raised.fill") { showApproval = true }
                    .padding(.horizontal, 20).padding(.bottom, 10)
                    .background(.ultraThinMaterial)
            } else if m.status == .failed {
                SilverButton(title: "Retry the upload", icon: "arrow.clockwise") {}
                    .padding(.horizontal, 20).padding(.bottom, 10)
                    .background(.ultraThinMaterial)
            }
        }
        .sheet(isPresented: $showApproval) {
            if let d = m.draft { ApprovalSheet(draft: d) }
        }
        .onAppear {
            app.hideTabBar = true
            if ProcessInfo.processInfo.environment["BRUCE_PRESENT"] == "approval" { showApproval = true }
        }
        .onDisappear { app.hideTabBar = false }
    }
}

// MARK: - Approval sheet (Bruce's trust moment)

struct ApprovalSheet: View {
    let draft: DraftEmail
    @Environment(\.dismiss) private var dismiss
    @State private var sent = false

    var body: some View {
        ZStack {
            Theme.Backdrop()
            if sent { sentState } else { reviewState }
        }
        .presentationDetents([.large])
        .preferredColorScheme(.dark)
    }

    private var reviewState: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Review before sending").font(.system(size: 18, weight: .bold)).foregroundStyle(Theme.text)
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark").font(.system(size: 14, weight: .bold))
                        .foregroundStyle(Theme.textSecondary).frame(width: 32, height: 32)
                        .background(Theme.surfaceHi, in: Circle())
                }.buttonStyle(.plain)
            }
            .padding(.horizontal, 18).padding(.top, 18).padding(.bottom, 12)

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    GlassCard {
                        VStack(alignment: .leading, spacing: 12) {
                            field("To", draft.to, sub: draft.toRole)
                            Divider().overlay(Theme.stroke)
                            field("Subject", draft.subject, sub: nil)
                            Divider().overlay(Theme.stroke)
                            Text(draft.body).font(.system(size: 14)).foregroundStyle(Theme.text)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        Label("Grounded on", systemImage: "checkmark.seal.fill")
                            .font(.subheadline.weight(.semibold)).foregroundStyle(.green)
                        ForEach(draft.grounded, id: \.self) { g in
                            HStack(alignment: .top, spacing: 8) {
                                Image(systemName: "checkmark").font(.caption2.weight(.bold)).foregroundStyle(.green)
                                    .padding(.top, 3)
                                Text(g).font(.caption).foregroundStyle(Theme.textSecondary)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
                    .background(Color.green.opacity(0.06), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Color.green.opacity(0.18)))

                    Color.clear.frame(height: 120)
                }
                .padding(.horizontal, 18)
            }
            .scrollIndicators(.hidden)
        }
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 10) {
                SilverButton(title: "Approve & send", icon: "paperplane.fill") {
                    Haptics.success()
                    withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) { sent = true }
                }
                HStack(spacing: 10) {
                    GhostButton(title: "Edit", icon: "pencil") {}
                    GhostButton(title: "Decline", icon: "xmark") { dismiss() }
                }
            }
            .padding(.horizontal, 18).padding(.bottom, 12)
            .background(.ultraThinMaterial)
        }
    }

    private var sentState: some View {
        VStack(spacing: 16) {
            Spacer()
            ZStack {
                Circle().fill(Color.green.opacity(0.14)).frame(width: 92, height: 92)
                Image(systemName: "checkmark").font(.system(size: 40, weight: .bold)).foregroundStyle(.green)
            }
            Text("Sent to Prof. Huo").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.text)
            Text("Bruce will confirm delivery and watch for a reply.\nYou'll see it in your activity.")
                .font(.subheadline).foregroundStyle(Theme.textSecondary).multilineTextAlignment(.center)
            Spacer()
            SilverButton(title: "Done") { dismiss() }.padding(.horizontal, 18).padding(.bottom, 16)
        }
        .padding(.horizontal, 18)
    }

    private func field(_ label: String, _ value: String, sub: String?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(Theme.textTertiary)
            Text(value).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
            if let sub { Text(sub).font(.caption).foregroundStyle(Theme.textSecondary) }
        }
    }
}

// MARK: - Handoff / capture sheet

struct HandoffSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var parsing = false
    @State private var parsed = false
    @State private var clarify = Demo.present == "clarify"

    private let sources: [(String, String, String)] = [
        ("text.viewfinder", "Paste text", "A deadline, a flyer, an assignment"),
        ("doc.fill", "Attach a PDF", "Forms, syllabi, program pages"),
        ("photo.fill", "Add a photo", "Snap a poster or handout"),
        ("link", "Paste a link", "An opportunity or program site"),
    ]

    var body: some View {
        ZStack {
            Theme.Backdrop()
            VStack(spacing: 0) {
                Capsule().fill(Theme.strokeHi).frame(width: 38, height: 5).padding(.top, 10)
                VStack(alignment: .leading, spacing: 6) {
                    Text("Hand it to Bruce").font(.system(size: 24, weight: .bold)).foregroundStyle(Theme.text)
                    Text("Forward anything school-related. Bruce figures out what it is and starts a mission.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 20).padding(.top, 18).padding(.bottom, 18)

                if clarify { clarifyState }
                else if parsed { parsedState }
                else if parsing { parsingState }
                else { pickerState }
                Spacer()
            }
        }
        .presentationDetents([.medium, .large])
        .preferredColorScheme(.dark)
    }

    private var pickerState: some View {
        VStack(spacing: 12) {
            ForEach(sources, id: \.0) { s in
                Button {
                    withAnimation { parsing = true }
                } label: {
                    HStack(spacing: 14) {
                        Image(systemName: s.0).font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(Theme.silver).frame(width: 46, height: 46)
                            .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Theme.stroke))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(s.1).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                            Text(s.2).font(.caption).foregroundStyle(Theme.textSecondary)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.caption.weight(.bold)).foregroundStyle(Theme.textTertiary)
                    }
                    .padding(14)
                    .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Theme.silverEdge))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 20)
    }

    private var parsingState: some View {
        VStack(spacing: 16) {
            ProgressView().tint(Theme.text).scaleEffect(1.3).padding(.top, 30)
            Text("Bruce is reading it…").font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
            Text("Extracting the deadline, the ask, and what you'll need.")
                .font(.subheadline).foregroundStyle(Theme.textSecondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 30)
        .onAppear {
            // Mock parse delay handled by RootView timer-free approach: flip after appear.
            withAnimation(.easeInOut(duration: 0.6).delay(1.1)) { parsed = true }
        }
    }

    private var parsedState: some View {
        VStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 14) {
                Label("Bruce understood this", systemImage: "sparkles")
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.silver)
                understoodRow("Type", "Program deadline")
                understoodRow("Deadline", "March 15 · 5:00 PM")
                understoodRow("Action", "Start an application mission")
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Theme.silverEdge))
            .padding(.horizontal, 20)

            SilverButton(title: "Start this mission", icon: "arrow.right") { dismiss() }
                .padding(.horizontal, 20)
        }
    }

    private func understoodRow(_ k: String, _ v: String) -> some View {
        HStack {
            Text(k).font(.subheadline).foregroundStyle(Theme.textSecondary)
            Spacer()
            Text(v).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
        }
    }

    private var clarifyState: some View {
        VStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 14) {
                Label("Bruce needs one thing", systemImage: "questionmark.circle.fill")
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Color(hex: 0xF5C451))
                Text("This flyer lists two deadlines. Which one is yours?")
                    .font(.system(size: 18, weight: .semibold)).foregroundStyle(Theme.text)
                VStack(spacing: 10) {
                    clarifyOption("Early submission — Mar 1")
                    clarifyOption("Final deadline — Mar 15")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Theme.silverEdge))
            .padding(.horizontal, 20)

            Text("Bruce only asks when it genuinely can't tell. Everything else it decides on its own.")
                .font(.caption).foregroundStyle(Theme.textTertiary).padding(.horizontal, 24)
        }
    }

    private func clarifyOption(_ t: String) -> some View {
        Button { withAnimation { clarify = false; parsed = true } } label: {
            HStack {
                Text(t).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                Spacer()
                Image(systemName: "chevron.right").font(.caption.weight(.bold)).foregroundStyle(Theme.textTertiary)
            }
            .padding(14)
            .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Theme.stroke))
        }.buttonStyle(.plain)
    }
}

// MARK: - Missions tab

struct MissionsView: View {
    @State private var filter = 0
    private let filters = ["All", "Needs you", "Working", "Done"]

    private var shown: [Mission] {
        switch filter {
        case 1: return Mock.needsYou
        case 2: return Mock.working
        case 3: return []
        default: return Mock.missions
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Missions").font(.system(size: 30, weight: .bold)).foregroundStyle(Theme.text)
                        .padding(.top, 6)

                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(Array(filters.enumerated()), id: \.offset) { i, f in
                                Button { withAnimation(.easeInOut(duration: 0.2)) { filter = i } } label: {
                                    Text(f).font(.subheadline.weight(.semibold))
                                        .foregroundStyle(filter == i ? Theme.bg : Theme.textSecondary)
                                        .padding(.horizontal, 14).padding(.vertical, 8)
                                        .background(filter == i ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.surfaceHi), in: Capsule())
                                }.buttonStyle(.plain)
                            }
                        }
                    }

                    if Demo.state == "empty" || shown.isEmpty {
                        EmptyStateView(icon: "tray",
                                       title: filter == 3 ? "Nothing done yet" : "No missions yet",
                                       message: "Hand Bruce something — a flyer, a deadline, an email — and it'll start one.")
                    } else if Demo.state == "loading" {
                        ForEach(0..<3, id: \.self) { _ in MissionSkeleton() }
                    } else {
                        VStack(spacing: 12) {
                            ForEach(shown) { m in
                                NavigationLink { MissionDetailView(m: m) } label: { MissionListRow(m: m) }
                                    .buttonStyle(.plain)
                            }
                        }
                    }
                    Color.clear.frame(height: 96)
                }
                .padding(.horizontal, 20)
            }
            .scrollIndicators(.hidden)
            .background(Theme.Backdrop())
        }
    }
}

// MARK: - Calendar tab

struct CalendarView: View {
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Dates").font(.system(size: 30, weight: .bold)).foregroundStyle(Theme.text)
                    Text("Bruce finds dates. You decide what gets added.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }
                .padding(.top, 6)

                VStack(spacing: 12) {
                    ForEach(Mock.calendar) { c in
                        HStack(spacing: 14) {
                            VStack(spacing: 2) {
                                Text(c.mon).font(.caption2.weight(.bold)).foregroundStyle(Theme.textSecondary)
                                Text(c.num).font(.system(size: 20, weight: .bold, design: .rounded)).foregroundStyle(Theme.silver)
                            }
                            .frame(width: 52).padding(.vertical, 8)
                            .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 12, style: .continuous))

                            VStack(alignment: .leading, spacing: 4) {
                                Text(c.title).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text).lineLimit(1)
                                Text("\(c.time) · \(c.source)").font(.caption).foregroundStyle(Theme.textSecondary).lineLimit(1)
                                HStack(spacing: 5) {
                                    Image(systemName: c.state.symbol).font(.system(size: 11, weight: .bold))
                                    Text(c.state.text).font(.caption2.weight(.semibold))
                                }
                                .foregroundStyle(c.state.color)
                                .padding(.top, 1)
                            }
                            Spacer(minLength: 4)
                        }
                        .padding(16)
                        .glass(18)
                    }
                }
                Color.clear.frame(height: 96)
            }
            .padding(.horizontal, 20)
        }
        .scrollIndicators(.hidden)
    }
}

// MARK: - Decisions tab

struct DecisionsView: View {
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("Decisions").font(.system(size: 30, weight: .bold)).foregroundStyle(Theme.text)
                        Text("\(Mock.decisions.count)").font(.system(size: 18, weight: .bold)).foregroundStyle(Theme.amber)
                    }
                    Text("The only things Bruce needs you for. Everything else it handles.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }
                .padding(.top, 6)

                VStack(spacing: 12) {
                    ForEach(Mock.decisions) { d in DecisionRow(d: d) }
                }
                Color.clear.frame(height: 96)
            }
            .padding(.horizontal, 20)
        }
        .scrollIndicators(.hidden)
    }
}

struct DecisionRow: View {
    let d: Decision
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(d.title).font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
                    Text(d.source).font(.caption).foregroundStyle(Theme.textTertiary)
                }
                Spacer()
                Menu {
                    Button { } label: { Label("Remind me later", systemImage: "clock") }
                    Button(role: .destructive) { } label: { Label("Dismiss", systemImage: "xmark") }
                } label: {
                    Image(systemName: "ellipsis").font(.system(size: 15, weight: .bold)).foregroundStyle(Theme.textTertiary)
                        .frame(width: 30, height: 30)
                }
            }
            Text(d.context).font(.subheadline).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if let detail = d.detail {
                Text(detail).font(.caption).foregroundStyle(Theme.textTertiary)
                    .padding(.vertical, 8).padding(.horizontal, 12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            }
            Button { Haptics.tap() } label: {
                Text(d.cta).font(.system(size: 15, weight: .bold)).foregroundStyle(Theme.bg)
                    .padding(.vertical, 11).padding(.horizontal, 22)
                    .background(Theme.silver, in: Capsule())
            }.buttonStyle(PressStyle())
        }
        .padding(16)
        .glass(18)
    }
}

// MARK: - You / settings tab

struct YouView: View {
    @State private var showDelete = Demo.present == "delete"
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(spacing: 14) {
                    Text("D").font(.system(size: 26, weight: .bold, design: .rounded)).foregroundStyle(Theme.bg)
                        .frame(width: 64, height: 64).background(Theme.silver, in: Circle())
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Dhruv Jain").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.text)
                        Text("High school junior · 11th grade").font(.subheadline).foregroundStyle(Theme.textSecondary)
                    }
                    Spacer()
                }
                .padding(.top, 6)

                settingsGroup("Your data", [
                    ("lock.fill", "Privacy & what Bruce stores", true),
                    ("clock.arrow.circlepath", "Auto-delete forwarded content", true),
                    ("square.and.arrow.up", "Export everything", false),
                ])
                settingsGroup("Bruce", [
                    ("bell.fill", "Notifications", true),
                    ("envelope.fill", "Connected accounts", true),
                    ("questionmark.circle.fill", "Help", false),
                ])

                Button { showDelete = true } label: {
                    HStack {
                        Image(systemName: "trash.fill")
                        Text("Delete my account & all data").font(.system(size: 16, weight: .semibold))
                        Spacer()
                    }
                    .foregroundStyle(Color(hex: 0xFF6B6B))
                    .padding(16)
                    .background(Color(hex: 0xFF6B6B).opacity(0.08), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Color(hex: 0xFF6B6B).opacity(0.2)))
                }.buttonStyle(.plain)

                Text("Bruce keeps forwarded content only as long as a mission needs it, then deletes it.")
                    .font(.caption).foregroundStyle(Theme.textTertiary)
                Color.clear.frame(height: 96)
            }
            .padding(.horizontal, 18)
        }
        .scrollIndicators(.hidden)
        .sheet(isPresented: $showDelete) { DeleteAccountSheet() }
    }

    private func settingsGroup(_ title: String, _ rows: [(String, String, Bool)]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.system(size: 19, weight: .bold)).foregroundStyle(Theme.text)
            GlassCard(padding: 6) {
                VStack(spacing: 0) {
                    ForEach(Array(rows.enumerated()), id: \.offset) { i, r in
                        HStack(spacing: 13) {
                            Image(systemName: r.0).font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.silver)
                                .frame(width: 34, height: 34)
                                .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                            Text(r.1).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
                            Spacer()
                            Image(systemName: "chevron.right").font(.caption.weight(.bold)).foregroundStyle(Theme.textTertiary)
                        }
                        .padding(.horizontal, 10).padding(.vertical, 12)
                        if i < rows.count - 1 { Divider().overlay(Theme.stroke).padding(.leading, 57) }
                    }
                }
            }
        }
    }
}
