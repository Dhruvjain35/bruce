import SwiftUI

// MARK: - Root flow (onboarding gate)

struct RootFlow: View {
    // Onboarding shows on every launch for now (testing). BRUCE_SKIP_ONBOARD=1 jumps straight to the app.
    @State private var onboarded = Demo.env["BRUCE_SKIP_ONBOARD"] == "1"

    var body: some View {
        if onboarded {
            RootView()
        } else {
            OnboardingView { withAnimation(.easeInOut) { onboarded = true } }
        }
    }
}

// MARK: - Onboarding (functional setup, not a marketing carousel)

struct OnboardingView: View {
    let onDone: () -> Void
    @State private var step = Int(Demo.env["BRUCE_ONBOARD_STEP"] ?? "") ?? 0
    private let steps = 7

    // mock selections
    @State private var grade = "11th grade"
    @State private var gradYear = "2027"
    @State private var focus: Set<String> = ["Deadlines and assignments", "Opportunities and scholarships"]

    var body: some View {
        ZStack {
            Theme.Backdrop()
            VStack(spacing: 0) {
                stepBar
                Group {
                    switch step {
                    case 0: identity
                    case 1: context
                    case 2: focusPick
                    case 3: connectCalendar
                    case 4: optionalSystems
                    case 5: notifications
                    default: firstAction
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .preferredColorScheme(.dark)
    }

    private func advance() { withAnimation(.easeInOut(duration: 0.25)) { step += 1 } }

    private var stepBar: some View {
        HStack(spacing: 8) {
            if step > 0 {
                Button { withAnimation(.easeInOut(duration: 0.25)) { step -= 1 } } label: {
                    Image(systemName: "chevron.left").font(.system(size: 14, weight: .bold)).foregroundStyle(Theme.textSecondary)
                        .frame(width: 30, height: 30).background(Theme.surface, in: Circle())
                        .overlay(Circle().strokeBorder(Theme.stroke))
                }.buttonStyle(.plain)
            }
            HStack(spacing: 5) {
                ForEach(0..<steps, id: \.self) { i in
                    Capsule().fill(i <= step ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.surfaceHi))
                        .frame(height: 4)
                }
            }
        }
        .padding(.horizontal, 20).padding(.top, 14)
    }

    // MARK: steps

    private var identity: some View {
        VStack(spacing: 0) {
            Spacer()
            VStack(spacing: 20) {
                Image(systemName: "sparkles").font(.system(size: 46, weight: .semibold)).foregroundStyle(Theme.silver)
                    .frame(width: 110, height: 110)
                    .background(Theme.surfaceHi, in: Circle())
                    .overlay(Circle().strokeBorder(Theme.silverEdge))
                Text("Meet Bruce").font(.system(size: 30, weight: .bold)).foregroundStyle(Theme.text)
                Text("Your student-life assistant for deadlines, opportunities, applications, and school admin.")
                    .font(.system(size: 17)).foregroundStyle(Theme.textSecondary).multilineTextAlignment(.center)
                    .padding(.horizontal, 24)
            }
            Spacer(); Spacer()
            VStack(spacing: 12) {
                Button { Haptics.tap(); advance() } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "apple.logo").font(.system(size: 17, weight: .medium))
                        Text("Continue with Apple").font(.system(size: 17, weight: .semibold))
                    }
                    .foregroundStyle(.black).frame(maxWidth: .infinity).padding(.vertical, 15)
                    .background(.white, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                }.buttonStyle(PressStyle())
                Button { advance() } label: {
                    Text("Sign in").font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.textSecondary)
                }.buttonStyle(.plain).padding(.top, 2)
            }
            .padding(.horizontal, 24).padding(.bottom, 24)
        }
    }

    private var context: some View {
        stepScaffold(title: "Where are you in school?",
                     subtitle: "Just enough for Bruce to be useful. You can add more later.",
                     cta: "Continue", action: advance) {
            VStack(alignment: .leading, spacing: 20) {
                pickerGroup("Grade", ["9th", "10th", "11th", "12th"], selection: $grade, suffix: " grade")
                fieldStub("School name", "Your school (optional)")
                pickerGroup("Graduation year", ["2026", "2027", "2028", "2029"], selection: $gradYear, suffix: "")
                HStack {
                    Text("Time zone").font(.subheadline).foregroundStyle(Theme.textSecondary)
                    Spacer()
                    Text("Eastern Time · detected").font(.subheadline.weight(.medium)).foregroundStyle(Theme.text)
                }
            }
        }
    }

    private var focusPick: some View {
        stepScaffold(title: "What should Bruce handle first?",
                     subtitle: "This shapes your dashboard. Change it anytime.",
                     cta: "Continue", action: advance) {
            VStack(spacing: 10) {
                ForEach(Mock.focusAreas, id: \.self) { area in
                    let on = focus.contains(area)
                    Button {
                        Haptics.select()
                        if on { focus.remove(area) } else { focus.insert(area) }
                    } label: {
                        HStack {
                            Text(area).font(.system(size: 16, weight: .medium)).foregroundStyle(Theme.text)
                            Spacer()
                            Image(systemName: on ? "checkmark.circle.fill" : "circle")
                                .font(.system(size: 20)).foregroundStyle(on ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.textTertiary))
                        }
                        .padding(16)
                        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
                            .strokeBorder(on ? AnyShapeStyle(Theme.silver.opacity(0.4)) : AnyShapeStyle(Theme.stroke)))
                    }.buttonStyle(PressStyle())
                }
            }
        }
    }

    private var connectCalendar: some View {
        stepScaffold(title: "Connect your school calendar",
                     subtitle: "So Bruce can keep your deadlines straight.",
                     cta: nil, action: {}) {
            VStack(alignment: .leading, spacing: 16) {
                contract(canDo: ["Find conflicts", "Add approved deadlines", "Track upcoming events"],
                         cantDo: ["Delete existing events", "Invite people without your approval"])
                VStack(spacing: 10) {
                    SilverButton(title: "Connect Google Calendar", icon: "calendar") { advance() }
                    GhostButton(title: "Use Apple Calendar", icon: "calendar.badge.plus") { advance() }
                    Button { advance() } label: {
                        Text("Not now").font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.textSecondary)
                    }.buttonStyle(.plain).padding(.top, 2)
                }
            }
        }
    }

    private var optionalSystems: some View {
        stepScaffold(title: "Connect another source",
                     subtitle: "Optional. Bruce is honest about what's ready.",
                     cta: "Continue", action: advance) {
            VStack(spacing: 10) {
                ForEach(Mock.integrations) { i in
                    HStack(spacing: 13) {
                        Image(systemName: i.icon).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.silver)
                            .frame(width: 40, height: 40)
                            .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Theme.stroke))
                        Text(i.name).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
                        Spacer()
                        Text(i.status).font(.caption.weight(.semibold)).foregroundStyle(statusColor(i.status))
                    }
                    .padding(.vertical, 6)
                }
            }
        }
    }

    private var notifications: some View {
        stepScaffold(title: "Notifications that respect your time",
                     subtitle: "Bruce only notifies you when it matters:",
                     cta: nil, action: {}) {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 12) {
                    notifRow("A deadline is at risk")
                    notifRow("A mission needs your decision")
                    notifRow("Something is verified complete")
                    notifRow("Important information changes")
                }
                VStack(spacing: 10) {
                    SilverButton(title: "Enable important notifications", icon: "bell.fill") { advance() }
                    Button { advance() } label: {
                        Text("Not now").font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.textSecondary)
                    }.buttonStyle(.plain).padding(.top, 2)
                }
            }
        }
    }

    private var firstAction: some View {
        stepScaffold(title: "Hand Bruce your first item",
                     subtitle: "See it work before you're done. Bruce turns this into your first mission.",
                     cta: nil, action: {}) {
            VStack(spacing: 10) {
                firstOption("photo.fill", "Choose a screenshot")
                firstOption("doc.fill", "Import a PDF")
                firstOption("envelope.fill", "Forward an email")
                firstOption("sparkles", "Try a sample")
                Button { onDone() } label: {
                    Text("I'll do this later").font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.textSecondary)
                }.buttonStyle(.plain).padding(.top, 4)
            }
        }
    }

    // MARK: building blocks

    private func stepScaffold<Content: View>(title: String, subtitle: String, cta: String?, action: @escaping () -> Void,
                                             @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(title).font(.system(size: 26, weight: .bold)).foregroundStyle(Theme.text)
                        Text(subtitle).font(.system(size: 16)).foregroundStyle(Theme.textSecondary)
                    }
                    content()
                    Color.clear.frame(height: 8)
                }
                .padding(.horizontal, 22).padding(.top, 24)
            }
            .scrollIndicators(.hidden)
            if let cta {
                SilverButton(title: cta) { action() }
                    .padding(.horizontal, 22).padding(.bottom, 22).padding(.top, 6)
            }
        }
    }

    private func pickerGroup(_ label: String, _ options: [String], selection: Binding<String>, suffix: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.textSecondary)
            HStack(spacing: 8) {
                ForEach(options, id: \.self) { o in
                    let value = o + suffix
                    let on = selection.wrappedValue == value
                    Button { Haptics.select(); selection.wrappedValue = value } label: {
                        Text(o).font(.subheadline.weight(.semibold))
                            .foregroundStyle(on ? Theme.bg : Theme.textSecondary)
                            .frame(maxWidth: .infinity).padding(.vertical, 10)
                            .background(on ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.surface), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(on ? Color.clear : Theme.stroke))
                    }.buttonStyle(PressStyle())
                }
            }
        }
    }

    private func fieldStub(_ label: String, _ placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.textSecondary)
            Text(placeholder).font(.system(size: 16)).foregroundStyle(Theme.textTertiary)
                .frame(maxWidth: .infinity, alignment: .leading).padding(14)
                .background(Theme.surface, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Theme.stroke))
        }
    }

    private func contract(canDo: [String], cantDo: [String]) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Bruce can").font(.caption.weight(.bold)).foregroundStyle(Theme.green)
                ForEach(canDo, id: \.self) { c in
                    Label(c, systemImage: "checkmark").font(.subheadline).foregroundStyle(Theme.textSecondary)
                        .labelStyle(.titleAndIcon)
                }
            }
            Divider().overlay(Theme.stroke)
            VStack(alignment: .leading, spacing: 8) {
                Text("Bruce cannot").font(.caption.weight(.bold)).foregroundStyle(Theme.textTertiary)
                ForEach(cantDo, id: \.self) { c in
                    Label(c, systemImage: "xmark").font(.subheadline).foregroundStyle(Theme.textSecondary)
                        .labelStyle(.titleAndIcon)
                }
            }
        }
        .padding(16)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke))
    }

    private func notifRow(_ t: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "bell.fill").font(.caption).foregroundStyle(Theme.silver)
            Text(t).font(.system(size: 16)).foregroundStyle(Theme.text)
        }
    }

    private func firstOption(_ icon: String, _ title: String) -> some View {
        Button { onDone() } label: {
            HStack(spacing: 14) {
                Image(systemName: icon).font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.silver)
                    .frame(width: 46, height: 46)
                    .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Theme.stroke))
                Text(title).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                Spacer()
                Image(systemName: "chevron.right").font(.caption.weight(.bold)).foregroundStyle(Theme.textTertiary)
            }
            .padding(14)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke))
        }.buttonStyle(PressStyle())
    }

    private func statusColor(_ s: String) -> Color {
        switch s {
        case "Requires school approval": return Theme.amber
        case "Coming later": return Theme.textTertiary
        default: return Theme.textSecondary
        }
    }
}

// MARK: - Empty state

struct EmptyStateView: View {
    let icon: String
    let title: String
    let message: String
    var body: some View {
        VStack(spacing: 14) {
            ZStack {
                Circle().fill(Theme.surfaceHi).frame(width: 76, height: 76)
                    .overlay(Circle().strokeBorder(Theme.stroke))
                Image(systemName: icon).font(.system(size: 28, weight: .semibold)).foregroundStyle(Theme.silver)
            }
            Text(title).font(.system(size: 19, weight: .bold)).foregroundStyle(Theme.text)
            Text(message).font(.subheadline).foregroundStyle(Theme.textSecondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 70).padding(.horizontal, 30)
    }
}

// MARK: - Loading skeleton

struct MissionSkeleton: View {
    var body: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 14) {
                HStack { bar(170, 16); Spacer(); bar(84, 22) }
                bar(210, 12)
                bar(nil, 6)
            }
        }
        .shimmer()
    }
    private func bar(_ w: CGFloat?, _ h: CGFloat) -> some View {
        RoundedRectangle(cornerRadius: 6, style: .continuous)
            .fill(Theme.surfaceHi)
            .frame(width: w, height: h)
            .frame(maxWidth: w == nil ? .infinity : nil, alignment: .leading)
    }
}

// MARK: - Offline banner

struct OfflineBanner: View {
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "wifi.slash").font(.caption.weight(.bold))
            Text("You're offline — Bruce will sync when you're back")
                .font(.caption.weight(.semibold))
            Spacer()
        }
        .foregroundStyle(Theme.text)
        .padding(.horizontal, 18).padding(.vertical, 10)
        .background(Theme.surfaceHi)
        .overlay(Rectangle().fill(Theme.stroke).frame(height: 1), alignment: .bottom)
    }
}

// MARK: - Undo toast

struct Toast: View {
    let text: String
    let action: String
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            Text(text).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
            Spacer()
            Text(action).font(.system(size: 15, weight: .bold)).foregroundStyle(Theme.bg)
                .padding(.horizontal, 14).padding(.vertical, 6)
                .background(Theme.silver, in: Capsule())
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(Theme.strokeHi, lineWidth: 1))
        .padding(.horizontal, 18)
    }
}

// MARK: - Delete account confirmation

struct DeleteAccountSheet: View {
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        ZStack {
            Theme.Backdrop()
            VStack(spacing: 16) {
                Spacer()
                ZStack {
                    Circle().fill(Color(hex: 0xFF6B6B).opacity(0.14)).frame(width: 90, height: 90)
                    Image(systemName: "trash.fill").font(.system(size: 34)).foregroundStyle(Color(hex: 0xFF6B6B))
                }
                Text("Delete everything?").font(.system(size: 24, weight: .bold)).foregroundStyle(Theme.text)
                Text("This permanently deletes your account and everything Bruce stores for you. It can't be undone.")
                    .font(.subheadline).foregroundStyle(Theme.textSecondary).multilineTextAlignment(.center)

                VStack(alignment: .leading, spacing: 10) {
                    bullet("Every mission and draft")
                    bullet("Everything you forwarded to Bruce")
                    bullet("Your profile and settings")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
                .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke))

                Spacer()
                Button { } label: {
                    Text("Delete my account").font(.system(size: 16, weight: .bold)).foregroundStyle(.white)
                        .frame(maxWidth: .infinity).padding(.vertical, 15)
                        .background(Color(hex: 0xFF6B6B), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                }.buttonStyle(.plain)
                GhostButton(title: "Keep my account") { dismiss() }
            }
            .padding(.horizontal, 22).padding(.bottom, 16)
        }
        .presentationDetents([.large])
        .presentationBackground(Theme.bg)
        .preferredColorScheme(.dark)
    }
    private func bullet(_ t: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "xmark").font(.caption2.weight(.bold)).foregroundStyle(Color(hex: 0xFF6B6B)).padding(.top, 2)
            Text(t).font(.subheadline).foregroundStyle(Theme.textSecondary)
        }
    }
}
