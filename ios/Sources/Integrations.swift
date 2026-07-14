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

                // Add a source.
                Button { showAdd = true } label: {
                    HStack(spacing: 13) {
                        Image(systemName: "plus").font(.system(size: 16, weight: .bold)).foregroundStyle(Theme.silver)
                            .frame(width: 42, height: 42)
                            .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Add a source").font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                            Text("A website, calendar feed, forwarding rule, or folder").font(.caption).foregroundStyle(Theme.textSecondary)
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
            pill("Connect", fill: true) {}
        case "Requires school access":
            pill("Request access", fill: false, tint: Theme.amber) {}
        case "Import only":
            pill("Import", fill: false) {}
        default: // Coming later
            Text("Coming later").font(.caption.weight(.semibold)).foregroundStyle(Theme.textTertiary)
                .frame(maxWidth: 110, alignment: .trailing)
        }
    }

    private func pill(_ title: String, fill: Bool, tint: Color = Theme.textSecondary, action: @escaping () -> Void = {}) -> some View {
        Button { Haptics.tap(); action() } label: {
            Text(title).font(.system(size: 13, weight: .bold))
                .foregroundStyle(fill ? Theme.bg : tint)
                .padding(.vertical, 8).padding(.horizontal, 14)
                .background(fill ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.surfaceHi), in: Capsule())
                .overlay(Capsule().strokeBorder(fill ? Color.clear : tint.opacity(0.35)))
        }.buttonStyle(PressStyle())
    }
}

struct AddSourceKind: Identifiable {
    let id = UUID(); let name: String; let icon: String; let field: String; let placeholder: String; let keyboard: UIKeyboardType
}

struct AddCustomIntegrationView: View {
    @Environment(BruceStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var kind = 0
    @State private var name = ""
    @State private var value = ""

    private let kinds: [AddSourceKind] = [
        AddSourceKind(name: "Website", icon: "globe", field: "LINK", placeholder: "https://…", keyboard: .URL),
        AddSourceKind(name: "Calendar feed", icon: "calendar", field: "ICS URL", placeholder: "webcal://… or https://…​.ics", keyboard: .URL),
        AddSourceKind(name: "Forwarding rule", icon: "arrowshape.turn.up.right.fill", field: "FORWARD TO", placeholder: "you@school.edu → bruce", keyboard: .emailAddress),
        AddSourceKind(name: "Shared folder", icon: "folder.fill", field: "FOLDER LINK", placeholder: "https://…", keyboard: .URL),
        AddSourceKind(name: "RSS feed", icon: "dot.radiowaves.up.forward", field: "FEED URL", placeholder: "https://…/feed", keyboard: .URL),
    ]
    private var k: AddSourceKind { kinds[kind] }

    var body: some View {
        ZStack {
            Theme.Backdrop()
            VStack(spacing: 0) {
                HStack {
                    Text("Add a source").font(.system(size: 18, weight: .bold)).foregroundStyle(Theme.text)
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.system(size: 14, weight: .bold)).foregroundStyle(Theme.textSecondary)
                            .frame(width: 32, height: 32).background(Theme.surfaceHi, in: Circle())
                    }.buttonStyle(.plain)
                }
                .padding(.horizontal, 20).padding(.top, 20).padding(.bottom, 14)

                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        Text("Give Bruce a website, calendar feed, forwarding rule, or folder to watch.")
                            .font(.subheadline).foregroundStyle(Theme.textSecondary)

                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(Array(kinds.enumerated()), id: \.element.id) { i, kd in
                                    Button { Haptics.select(); kind = i } label: {
                                        HStack(spacing: 6) {
                                            Image(systemName: kd.icon).font(.system(size: 12, weight: .semibold))
                                            Text(kd.name).font(.subheadline.weight(.semibold))
                                        }
                                        .foregroundStyle(kind == i ? Theme.bg : Theme.textSecondary)
                                        .padding(.horizontal, 13).padding(.vertical, 9)
                                        .background(kind == i ? AnyShapeStyle(Theme.silver) : AnyShapeStyle(Theme.surfaceHi), in: Capsule())
                                    }.buttonStyle(PressStyle())
                                }
                            }
                        }

                        field("NAME", placeholder: "e.g. Robotics club site", text: $name)
                        field(k.field, placeholder: k.placeholder, text: $value, keyboard: k.keyboard)

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
            SilverButton(title: "Add source", icon: "checkmark") {
                let n = name.trimmingCharacters(in: .whitespaces)
                let u = value.trimmingCharacters(in: .whitespaces)
                if !n.isEmpty && !u.isEmpty { store.addCustomIntegration(name: "\(n) · \(k.name)", url: u); dismiss() }
            }
            .opacity(name.trimmingCharacters(in: .whitespaces).isEmpty || value.trimmingCharacters(in: .whitespaces).isEmpty ? 0.5 : 1)
            .padding(.horizontal, 20).padding(.top, 14).padding(.bottom, 12).background(Theme.bottomFade)
        }
    }

    private func field(_ label: String, placeholder: String, text: Binding<String>, keyboard: UIKeyboardType = .default) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 10, weight: .bold)).tracking(0.8).foregroundStyle(Theme.textTertiary)
            TextField("", text: text, prompt: Text(placeholder).foregroundColor(Theme.textTertiary))
                .font(.system(size: 16)).foregroundStyle(Theme.text).tint(Theme.silver)
                .keyboardType(keyboard).autocorrectionDisabled().textInputAutocapitalization(keyboard == .default ? .words : .never)
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(14).glass(16)
    }
}
