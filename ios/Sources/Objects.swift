import SwiftUI

// MARK: - Person (canonical faculty page Bruce constructed)

struct PersonView: View {
    let p: Person
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(p.name).font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text("\(p.role) · \(p.institution)").font(.system(size: 16)).foregroundStyle(Theme.textSecondary)
                    if p.verified {
                        HStack(spacing: 5) {
                            Image(systemName: "checkmark.seal.fill").font(.caption).foregroundStyle(Theme.green)
                            Text("Verified faculty page").font(.caption.weight(.semibold)).foregroundStyle(Theme.green)
                        }
                    }
                }

                Module(label: "Why this match") {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(spacing: 6) {
                            Image(systemName: "checkmark.seal.fill").font(.caption).foregroundStyle(Theme.green)
                            Text(p.relevance).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.green)
                        }
                        FlowChips(items: p.topics)
                    }
                }

                Module(label: "Verified sources") {
                    VStack(spacing: 12) {
                        Button { Haptics.tap() } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "globe").font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.silver)
                                    .frame(width: 40, height: 40).background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("FACULTY PAGE").font(.system(size: 10, weight: .bold)).tracking(0.8).foregroundStyle(Theme.textTertiary)
                                    Text(p.facultyURL).font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.text).lineLimit(1)
                                }
                                Spacer(minLength: 8)
                                Image(systemName: "arrow.up.right").font(.caption.weight(.bold)).foregroundStyle(Theme.textTertiary)
                            }
                        }.buttonStyle(PressStyle())
                        EvidenceRow(e: p.paper)
                    }
                }

                Module(label: "Alternate matches") {
                    VStack(spacing: 12) {
                        ForEach(p.alternates, id: \.self) { a in
                            Button { Haptics.tap() } label: {
                                HStack {
                                    Text(a).font(.system(size: 15)).foregroundStyle(Theme.textSecondary)
                                    Spacer()
                                    Image(systemName: "chevron.right").font(.footnote.weight(.bold)).foregroundStyle(Theme.textTertiary)
                                }
                            }.buttonStyle(PressStyle())
                        }
                    }
                }
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

/// Simple wrapping chip row.
struct FlowChips: View {
    let items: [String]
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(items, id: \.self) { t in
                Text(t).font(.caption.weight(.semibold)).foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .background(Theme.surfaceHi, in: Capsule())
            }
        }
    }
}

// MARK: - Decision (canonical page: choice, consequences, reversibility, actions)

struct DecisionDetailView: View {
    let d: Decision
    @Environment(BruceStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var showApproval = false
    @State private var goChoose = false
    @State private var goDates = false
    private var ctaIcon: String {
        switch d.cta {
        case "Review email": return "envelope"
        case "Review dates": return "calendar"
        case "Choose": return "person.2"
        default: return "arrow.right"
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(d.title).font(.system(size: 26, weight: .bold)).foregroundStyle(Theme.text)
                    Text(d.source).font(.subheadline).foregroundStyle(Theme.textTertiary)
                    Text(d.context).font(.system(size: 16)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let detail = d.detail {
                    Module(label: "Consequences") {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(detail.components(separatedBy: " · "), id: \.self) { part in
                                HStack(spacing: 8) {
                                    Image(systemName: "circle.fill").font(.system(size: 4)).foregroundStyle(Theme.textTertiary)
                                    Text(part).font(.subheadline).foregroundStyle(Theme.textSecondary)
                                }
                            }
                        }
                    }
                }
                Color.clear.frame(height: 80)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .hidesTabBar()
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 10) {
                SilverButton(title: d.cta, icon: ctaIcon) {
                    switch d.cta {
                    case "Review email": showApproval = true
                    case "Choose": goChoose = true
                    case "Review dates": goDates = true
                    default: break
                    }
                }
                GhostButton(title: "Not now") { dismiss() }
            }
            .padding(.horizontal, 20).padding(.bottom, 10)
            .background(Theme.bottomFade)
        }
        .sheet(isPresented: $showApproval) {
            if let draft = store.missions.first(where: { $0.draft != nil })?.draft {
                ApprovalSheet(draft: draft).environment(store)
            }
        }
        .navigationDestination(isPresented: $goChoose) { ChooseRecommenderView() }
        .navigationDestination(isPresented: $goDates) { DatesReviewView() }
    }
}

// MARK: - Date (canonical page)

struct DateDetailView: View {
    let c: CalProposal
    @Environment(\.dismiss) private var dismiss
    @State private var goCompare = false
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(c.title).font(.system(size: 26, weight: .bold)).foregroundStyle(Theme.text)
                    Text("\(c.day) · \(c.time)").font(.system(size: 16)).foregroundStyle(Theme.textSecondary)
                    HStack(spacing: 5) {
                        Image(systemName: c.state.symbol).font(.system(size: 12, weight: .bold))
                        Text(c.state.text).font(.subheadline.weight(.semibold))
                    }.foregroundStyle(c.state.color)
                }
                Module(label: "Details") {
                    VStack(alignment: .leading, spacing: 12) {
                        detailRow("Source", c.source)
                        Divider().overlay(Theme.stroke)
                        detailRow("Calendar", c.state == .added ? "Personal · Bruce" : "Not added")
                        if c.state == .conflict {
                            Divider().overlay(Theme.stroke)
                            detailRow("Conflict", "Chemistry Review · 3:30–4:15 PM")
                        }
                    }
                }
                Color.clear.frame(height: 80)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .hidesTabBar()
        .navigationDestination(isPresented: $goCompare) { ConflictCompareView(c: c) }
        .safeAreaInset(edge: .bottom) {
            Group {
                if c.state == .added {
                    HStack(spacing: 10) {
                        GhostButton(title: "Open in Calendar", icon: "calendar") { Haptics.tap() }
                        GhostButton(title: "Remove", icon: "trash") { dismiss() }
                    }
                } else if c.state == .conflict {
                    SilverButton(title: "Compare options", icon: "arrow.left.arrow.right") { goCompare = true }
                } else {
                    SilverButton(title: "Add to calendar", icon: "calendar.badge.plus") { dismiss() }
                }
            }
            .padding(.horizontal, 20).padding(.bottom, 10).background(Theme.bottomFade)
        }
    }
    private func detailRow(_ k: String, _ v: String) -> some View {
        HStack { Text(k).font(.subheadline).foregroundStyle(Theme.textSecondary); Spacer()
            Text(v).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text).multilineTextAlignment(.trailing) }
    }
}

// MARK: - Conflict resolution

struct ConflictCompareView: View {
    let c: CalProposal
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Resolve conflict").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text("These two overlap. Pick how to handle it — Bruce won't change anything on its own.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }
                eventCard("Existing", "Chemistry Review", "3:30 – 4:15 PM", Theme.textSecondary)
                eventCard("Proposed", c.title, c.time, Theme.amber)
                VStack(spacing: 12) {
                    option("Reschedule the call", "Bruce asks Prof. Huo for a later slot")
                    option("Keep both", "Accept the overlap")
                    option("Skip the call", "Don't add it")
                }
                Color.clear.frame(height: 20)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .hidesTabBar()
    }
    private func eventCard(_ tag: String, _ title: String, _ time: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(tag.uppercased()).font(.system(size: 10, weight: .bold)).tracking(0.8).foregroundStyle(color)
            Text(title).font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
            Text(time).font(.subheadline).foregroundStyle(Theme.textSecondary)
        }.frame(maxWidth: .infinity, alignment: .leading).padding(16).glass(18)
    }
    private func option(_ title: String, _ sub: String) -> some View {
        Button { Haptics.tap(); dismiss() } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                    Text(sub).font(.caption).foregroundStyle(Theme.textSecondary)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.footnote.weight(.bold)).foregroundStyle(Theme.textTertiary)
            }.padding(16).glass(16)
        }.buttonStyle(PressStyle())
    }
}

// MARK: - Receipt (verified completion)

struct ReceiptView: View {
    let r: Receipt
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(spacing: 10) {
                    Image(systemName: "checkmark.seal.fill").font(.system(size: 22)).foregroundStyle(Theme.green)
                    Text("Verified complete").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.text)
                }
                Module(label: "Receipt") {
                    VStack(alignment: .leading, spacing: 12) {
                        detailRow("Sent to", r.to)
                        Divider().overlay(Theme.stroke)
                        detailRow("Delivered", r.deliveredAt)
                        Divider().overlay(Theme.stroke)
                        detailRow("Note", r.note)
                    }
                }
                Color.clear.frame(height: 30)
            }
            .padding(.horizontal, 20).padding(.top, 8)
        }
        .scrollIndicators(.hidden)
        .background(Theme.Backdrop())
        .navigationBarTitleDisplayMode(.inline)
        .hidesTabBar()
    }
    private func detailRow(_ k: String, _ v: String) -> some View {
        HStack { Text(k).font(.subheadline).foregroundStyle(Theme.textSecondary); Spacer()
            Text(v).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text) }
    }
}
