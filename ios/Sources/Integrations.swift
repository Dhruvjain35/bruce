import SwiftUI

struct IntegrationsView: View {
    @Environment(BruceStore.self) private var store
    @State private var showAdd = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Integrations").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text("Connect Bruce to where your school life already lives. Bruce is honest about what's ready.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }

                // Custom integrations the student added.
                if !store.customIntegrations.isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        SectionLabel(text: "Your integrations")
                        VStack(spacing: 0) {
                            ForEach(Array(store.customIntegrations.enumerated()), id: \.element.id) { i, c in
                                HStack(spacing: 13) {
                                    Image(systemName: "link").font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.silver)
                                        .frame(width: 42, height: 42)
                                        .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(c.name).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                                        Text(c.url).font(.caption).foregroundStyle(Theme.textSecondary).lineLimit(1)
                                    }
                                    Spacer(minLength: 8)
                                    HStack(spacing: 5) {
                                        Image(systemName: "checkmark.circle.fill").font(.caption)
                                        Text("Added").font(.caption.weight(.semibold))
                                    }.foregroundStyle(Theme.green)
                                }
                                .padding(.horizontal, 10).padding(.vertical, 12)
                                if i < store.customIntegrations.count - 1 { Divider().overlay(Theme.stroke).padding(.leading, 66) }
                            }
                        }
                        .padding(6).glass(18)
                    }
                }

                // Add-your-own entry.
                Button { showAdd = true } label: {
                    HStack(spacing: 13) {
                        Image(systemName: "plus").font(.system(size: 16, weight: .bold)).foregroundStyle(Theme.silver)
                            .frame(width: 42, height: 42)
                            .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Add your own").font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                            Text("Any app or website with a link").font(.caption).foregroundStyle(Theme.textSecondary)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.footnote.weight(.bold)).foregroundStyle(Theme.textTertiary)
                    }
                    .padding(14).glass(18)
                }.buttonStyle(PressStyle())

                ForEach(Mock.integrationCatalog) { section in
                    VStack(alignment: .leading, spacing: 10) {
                        SectionLabel(text: section.title)
                        VStack(spacing: 0) {
                            ForEach(Array(section.items.enumerated()), id: \.element.id) { i, item in
                                row(item)
                                if i < section.items.count - 1 { Divider().overlay(Theme.stroke).padding(.leading, 66) }
                            }
                        }
                        .padding(6).glass(18)
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
        .sheet(isPresented: $showAdd) { AddCustomIntegrationView().environment(store) }
    }

    private func row(_ item: Mock.IntegrationItem) -> some View {
        HStack(spacing: 13) {
            Image(systemName: item.icon).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.silver)
                .frame(width: 42, height: 42)
                .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(item.name).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                Text(item.detail).font(.caption).foregroundStyle(Theme.textSecondary).lineLimit(1)
            }
            Spacer(minLength: 8)
            trailing(item)
        }
        .padding(.horizontal, 10).padding(.vertical, 12)
    }

    @ViewBuilder private func trailing(_ item: Mock.IntegrationItem) -> some View {
        switch item.status {
        case "Connected":
            HStack(spacing: 5) {
                Image(systemName: "checkmark.circle.fill").font(.caption)
                Text("Connected").font(.caption.weight(.semibold))
            }.foregroundStyle(Theme.green)
        case "Available":
            Button { Haptics.tap() } label: {
                Text("Connect").font(.system(size: 14, weight: .bold)).foregroundStyle(Theme.bg)
                    .padding(.vertical, 8).padding(.horizontal, 16).background(Theme.silver, in: Capsule())
            }.buttonStyle(PressStyle())
        default:
            Text(item.status).font(.caption.weight(.semibold)).foregroundStyle(Mock.integrationColor(item.status))
                .multilineTextAlignment(.trailing).frame(maxWidth: 120, alignment: .trailing)
        }
    }
}

struct AddCustomIntegrationView: View {
    @Environment(BruceStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var url = ""

    var body: some View {
        ZStack {
            Theme.Backdrop()
            VStack(spacing: 0) {
                HStack {
                    Text("Add integration").font(.system(size: 18, weight: .bold)).foregroundStyle(Theme.text)
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.system(size: 14, weight: .bold)).foregroundStyle(Theme.textSecondary)
                            .frame(width: 32, height: 32).background(Theme.surfaceHi, in: Circle())
                    }.buttonStyle(.plain)
                }
                .padding(.horizontal, 20).padding(.top, 20).padding(.bottom, 14)

                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        Text("Give Bruce any app or website. When you say “do this in \(name.isEmpty ? "…" : name),” Bruce opens this link and gets to work.")
                            .font(.subheadline).foregroundStyle(Theme.textSecondary)

                        field("APP OR WEBSITE NAME", placeholder: "e.g. Naviance", text: $name)
                        field("LINK", placeholder: "https://…", text: $url, keyboard: .URL)

                        Color.clear.frame(height: 90)
                    }
                    .padding(.horizontal, 20)
                }
                .scrollIndicators(.hidden)
            }
        }
        .presentationDetents([.medium, .large])
        .presentationBackground(Theme.bg)
        .preferredColorScheme(.dark)
        .safeAreaInset(edge: .bottom) {
            SilverButton(title: "Add integration", icon: "checkmark") {
                let n = name.trimmingCharacters(in: .whitespaces)
                let u = url.trimmingCharacters(in: .whitespaces)
                if !n.isEmpty && !u.isEmpty { store.addCustomIntegration(name: n, url: u); dismiss() }
            }
            .opacity(name.trimmingCharacters(in: .whitespaces).isEmpty || url.trimmingCharacters(in: .whitespaces).isEmpty ? 0.5 : 1)
            .padding(.horizontal, 20).padding(.top, 14).padding(.bottom, 12).background(Theme.bottomFade)
        }
    }

    private func field(_ label: String, placeholder: String, text: Binding<String>, keyboard: UIKeyboardType = .default) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 10, weight: .bold)).tracking(0.8).foregroundStyle(Theme.textTertiary)
            TextField("", text: text, prompt: Text(placeholder).foregroundColor(Theme.textTertiary))
                .font(.system(size: 16)).foregroundStyle(Theme.text).tint(Theme.silver)
                .keyboardType(keyboard).autocorrectionDisabled().textInputAutocapitalization(keyboard == .URL ? .never : .words)
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(14).glass(16)
    }
}
