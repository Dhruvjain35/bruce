import SwiftUI

/// Small uppercase section label — carries hierarchy so cards don't have to shout.
struct SectionLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 12, weight: .bold)).tracking(1.4)
            .foregroundStyle(Theme.textTertiary)
    }
}

/// State expressed in words, with a thin accent only when it means something.
struct StatusLine: View {
    let status: Status
    let text: String
    var body: some View {
        HStack(spacing: 5) {
            if let s = status.symbol {
                Image(systemName: s).font(.system(size: 11, weight: .bold)).foregroundStyle(status.accent)
            }
            Text(text).font(.subheadline.weight(.medium))
                .foregroundStyle(status == .working ? Theme.textSecondary : status.accent)
        }
    }
}

/// Bruce's signature: the handoff command bar. The hero glass object on Home.
struct HandoffBar: View {
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "tray.and.arrow.down.fill")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(Theme.silver)
            Text("Hand something to Bruce…")
                .font(.system(size: 16, weight: .medium))
                .foregroundStyle(Theme.textSecondary)
            Spacer()
            Image(systemName: "paperclip")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Theme.textSecondary)
                .frame(width: 40, height: 40)
                .background(Theme.surfaceHi, in: RoundedRectangle(cornerRadius: 13, style: .continuous))
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 13)
        .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(Theme.silver.opacity(0.30), lineWidth: 1))
        .shadow(color: .black.opacity(0.30), radius: 16, x: 0, y: 9)
    }
}

/// Tappable glass chip for the Home "Today" row.
struct TodayChip: View {
    let n: Int
    let label: String
    var body: some View {
        HStack(spacing: 7) {
            Text("\(n)").font(.system(size: 20, weight: .bold, design: .rounded)).foregroundStyle(Theme.silver)
            Text(label).font(.subheadline.weight(.medium)).foregroundStyle(Theme.textSecondary)
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .glass(15)
    }
}

/// Light, tappable glass row for Home sections — title + one contextual line.
struct HomeMissionRow: View {
    let m: Mission
    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(m.title).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.text)
                Text(m.homeLine).font(.subheadline)
                    .foregroundStyle(m.status == .failed ? Theme.red : Theme.textSecondary)
                    .lineLimit(1)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.footnote.weight(.bold)).foregroundStyle(Theme.textTertiary)
        }
        .padding(.vertical, 15).padding(.horizontal, 16)
        .glass(16)
    }
}

/// Fuller glass row for the Missions list — title plus quiet current state (details live inside).
struct MissionListRow: View {
    let m: Mission
    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text(m.title).font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
                StatusLine(status: m.status, text: m.statusText)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.footnote.weight(.bold)).foregroundStyle(Theme.textTertiary)
        }
        .padding(18)
        .glass(18)
    }
}
