import SwiftUI

struct IntegrationsView: View {
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Integrations").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
                    Text("Connect Bruce to where your school life already lives. Bruce is honest about what's ready.")
                        .font(.subheadline).foregroundStyle(Theme.textSecondary)
                }

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
