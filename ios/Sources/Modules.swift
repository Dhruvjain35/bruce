import SwiftUI

/// Section label + a glass body. Every mission-workspace block is built from this.
struct Module<Content: View>: View {
    let label: String
    var body2: Bool = true
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: label)
            content
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
                .glass(18)
        }
    }
}

private func kv(_ k: String, _ v: String) -> some View {
    VStack(alignment: .leading, spacing: 2) {
        Text(k.uppercased()).font(.system(size: 10, weight: .bold)).tracking(0.8).foregroundStyle(Theme.textTertiary)
        Text(v).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
    }
}

// MARK: - Current action (the dominant NOW module)

struct ActionModule: View {
    let now: String
    let count: MissionCount?
    var primaryTitle: String? = nil
    var primaryIcon: String = "arrow.right"
    var primaryAction: () -> Void = {}
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "Now")
            VStack(alignment: .leading, spacing: 14) {
                Text(now).font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.text)
                    .fixedSize(horizontal: false, vertical: true)
                if let c = count {
                    Text("\(c.done) of \(c.total) \(c.noun) collected")
                        .font(.subheadline.weight(.medium)).foregroundStyle(Theme.silver)
                }
                if let t = primaryTitle {
                    SilverButton(title: t, icon: primaryIcon) { primaryAction() }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(18)
            .glass(18)
        }
    }
}

// MARK: - Draft email

struct DraftEmailModule: View {
    let draft: DraftEmail
    let onReview: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Draft email")
            Button { Haptics.tap(); onReview() } label: {
                VStack(alignment: .leading, spacing: 12) {
                    kv("To", draft.to)
                    Divider().overlay(Theme.stroke)
                    kv("Subject", draft.subject)
                    Text(draft.body).font(.system(size: 13)).foregroundStyle(Theme.textSecondary)
                        .lineLimit(3).padding(.top, 2)
                    HStack(spacing: 6) {
                        Image(systemName: "checkmark.seal.fill").font(.caption).foregroundStyle(Theme.green)
                        Text("Grounded in \(draft.grounded.count) verified sources").font(.caption).foregroundStyle(Theme.textSecondary)
                        Spacer()
                        Text("Tap to review").font(.caption.weight(.semibold)).foregroundStyle(Theme.textTertiary)
                        Image(systemName: "chevron.right").font(.caption2.weight(.bold)).foregroundStyle(Theme.textTertiary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
                .glass(18)
            }.buttonStyle(PressStyle())
        }
    }
}

// MARK: - Document checklist

struct ChecklistModule: View {
    let count: MissionCount?
    let items: [DocItem]
    var body: some View {
        Module(label: count.map { "Documents — \($0.done) of \($0.total)" } ?? "Documents") {
            VStack(spacing: 14) {
                ForEach(items) { d in
                    HStack(spacing: 12) {
                        Image(systemName: d.done ? "checkmark.circle.fill" : "circle")
                            .font(.system(size: 18))
                            .foregroundStyle(d.done ? AnyShapeStyle(Theme.green) : AnyShapeStyle(Theme.textTertiary))
                        VStack(alignment: .leading, spacing: 1) {
                            Text(d.name).font(.system(size: 15, weight: .medium))
                                .foregroundStyle(d.done ? Theme.textSecondary : Theme.text)
                            if !d.done { Text(d.note).font(.caption).foregroundStyle(Theme.amber) }
                            else { Text(d.note).font(.caption).foregroundStyle(Theme.textTertiary) }
                        }
                        Spacer()
                    }
                }
            }
        }
    }
}

// MARK: - After-approval plan (Handoff Contract, interactive)

struct AfterModule: View {
    let steps: [String]
    let followUp: FollowUp?
    let onEdit: () -> Void
    var body: some View {
        Module(label: "After approval") {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(Array(steps.enumerated()), id: \.offset) { i, s in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("\(i + 1)").font(.caption.weight(.bold)).foregroundStyle(Theme.bg)
                            .frame(width: 20, height: 20).background(Theme.silver, in: Circle())
                        Text(s).font(.system(size: 15)).foregroundStyle(Theme.textSecondary)
                    }
                }
                if let f = followUp {
                    Divider().overlay(Theme.stroke)
                    Text("Wait \(f.waitDays) days · at most \(f.maxFollowUps) follow-up · \(f.stopCondition.lowercased())")
                        .font(.caption).foregroundStyle(Theme.textTertiary)
                }
                Button { Haptics.tap(); onEdit() } label: {
                    Text("Edit plan").font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                        .padding(.vertical, 11).padding(.horizontal, 18)
                        .background(Theme.surfaceHi, in: Capsule())
                }.buttonStyle(PressStyle())
            }
        }
    }
}

// MARK: - Recovery (failure)

struct RecoveryModule: View {
    var onConvert: () -> Void = {}
    var body: some View {
        Module(label: "Recovery") {
            VStack(alignment: .leading, spacing: 12) {
                Text("The school portal rejected the file type.")
                    .font(.subheadline).foregroundStyle(Theme.textSecondary)
                SilverButton(title: "Convert and retry", icon: "arrow.clockwise") { onConvert() }
                HStack(spacing: 10) {
                    GhostButton(title: "Another file", icon: "doc") {}
                    GhostButton(title: "Open portal", icon: "safari") {}
                }
            }
        }
    }
}

// MARK: - Timeline

struct TimelineModule: View {
    let events: [TimelineEvent]
    var body: some View {
        Module(label: "Timeline") {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(events) { e in
                    HStack(alignment: .top, spacing: 12) {
                        Text(e.time).font(.caption.weight(.semibold)).foregroundStyle(Theme.textTertiary)
                            .frame(width: 58, alignment: .leading)
                        Text(e.text).font(.subheadline).foregroundStyle(Theme.textSecondary)
                        Spacer()
                    }
                }
            }
        }
    }
}

// MARK: - Faculty match (links to the canonical Person page)

struct PersonModule: View {
    let p: Person
    var body: some View {
        Module(label: "Faculty match") {
            NavigationLink { PersonView(p: p) } label: {
                HStack(spacing: 12) {
                    Image(systemName: "person.crop.rectangle.fill").font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(Theme.silver).frame(width: 44, height: 44)
                        .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p.name).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                        Text("\(p.role) · \(p.institution)").font(.caption).foregroundStyle(Theme.textSecondary).lineLimit(1)
                        HStack(spacing: 5) {
                            Image(systemName: "checkmark.seal.fill").font(.system(size: 10, weight: .bold)).foregroundStyle(Theme.green)
                            Text(p.relevance).font(.caption2.weight(.semibold)).foregroundStyle(Theme.green)
                        }.padding(.top, 1)
                    }
                    Spacer()
                    Image(systemName: "chevron.right").font(.footnote.weight(.bold)).foregroundStyle(Theme.textTertiary)
                }
            }.buttonStyle(PressStyle())
        }
    }
}

// MARK: - Evidence (links to sources)

struct EvidenceModule: View {
    let evidence: [EvidenceSource]
    var body: some View {
        Module(label: "Grounded in") {
            VStack(spacing: 4) { ForEach(evidence) { EvidenceRow(e: $0) } }
        }
    }
}
