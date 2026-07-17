import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import UIKit

/// The real capture surface. Same designed sheet as before (graphite, restrained glass, system type,
/// the four source cards) — but every action drives the REAL async backend via LiveIntakeSession.
/// There are no mock success states: submit → durable mission (202, ~50ms) → hand off to the canonical
/// mission detail, which polls to grounded results or a real failure.
struct HandoffSheet: View {
    /// Called the instant the mission is durably acknowledged, with the session already polling, so
    /// the presenter can push the canonical mission detail.
    var onStarted: (LiveIntakeSession) -> Void = { _ in }

    @Environment(\.dismiss) private var dismiss
    @State private var session = LiveIntakeSession()
    @State private var mode: Mode = .pick
    @State private var draft = ""
    @State private var link = ""
    @State private var photo: PhotosPickerItem? = nil
    @State private var showPDFImporter = false
    @State private var localError: String? = nil
    @State private var handedOff = false

    enum Mode { case pick, text, link }

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
                titleBlock
                content
                Spacer(minLength: 0)
            }
        }
        .presentationDetents([.medium, .large])
        .presentationBackground(Theme.bg)
        .preferredColorScheme(.dark)
        .photosPicker(isPresented: $photoPickerPresented, selection: $photo, matching: .images)
        .fileImporter(isPresented: $showPDFImporter, allowedContentTypes: [.pdf]) { handlePDF($0) }
        .onChange(of: photo) { _, item in Task { await handlePhoto(item) } }
        .onChange(of: session.stage) { _, s in
            // The instant the mission is durably acknowledged, hand off to the canonical detail.
            if s == .working, session.missionID != nil, !handedOff {
                handedOff = true
                onStarted(session)
                dismiss()
            }
        }
        .onAppear { Analytics.track(.sheetOpened) }
        .onDisappear { if !handedOff { session.trackAbandon() } }
    }

    private var titleBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Hand it to Bruce").font(.system(size: 24, weight: .bold)).foregroundStyle(Theme.text)
                .accessibilityAddTraits(.isHeader)
            Text("Forward anything school-related. Bruce figures out what it is and starts a mission.")
                .font(.subheadline).foregroundStyle(Theme.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 20).padding(.top, 18).padding(.bottom, 18)
    }

    @ViewBuilder private var content: some View {
        switch session.stage {
        case .submitting: sendingState
        case .failed, .sessionExpired: problemState
        default:
            switch mode {
            case .pick: pickerState
            case .text: textEntry(isLink: false)
            case .link: textEntry(isLink: true)
            }
        }
    }

    // MARK: source picker (unchanged design)

    private var pickerState: some View {
        VStack(spacing: 12) {
            ForEach(sources, id: \.0) { s in
                Button { select(s.1) } label: { sourceCard(s) }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(s.1). \(s.2)")
            }
        }
        .padding(.horizontal, 20)
    }

    private func sourceCard(_ s: (String, String, String)) -> some View {
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

    private func select(_ title: String) {
        localError = nil
        switch title {
        case "Paste text":   Analytics.track(.sourceSelected(.text)); mode = .text
        case "Paste a link": Analytics.track(.sourceSelected(.link)); mode = .link
        case "Add a photo":  Analytics.track(.sourceSelected(.photo)); triggerPhoto()
        case "Attach a PDF": Analytics.track(.sourceSelected(.pdf)); showPDFImporter = true
        default: break
        }
    }

    // MARK: text / link entry (keyboard-safe; cancel returns to the picker)

    private func textEntry(isLink: Bool) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Button { Haptics.tap(); mode = .pick; localError = nil } label: {
                        Label("Back", systemImage: "chevron.left").font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(Theme.textSecondary)
                    }.buttonStyle(.plain).accessibilityLabel("Back to source picker")
                    Spacer()
                }
                TextField("", text: isLink ? $link : $draft,
                          prompt: Text(isLink ? "Paste a link…" : "Paste or type…").foregroundColor(Theme.textTertiary),
                          axis: .vertical)
                    .font(.system(size: 16)).foregroundStyle(Theme.text).tint(Theme.silver)
                    .textInputAutocapitalization(isLink ? .never : .sentences)
                    .keyboardType(isLink ? .URL : .default)
                    .lineLimit(isLink ? 1...3 : 3...12)
                    .padding(14)
                    .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.silverEdge))
                    .accessibilityLabel(isLink ? "Link to hand to Bruce" : "Text to hand to Bruce")
                if let e = localError {
                    Text(e).font(.caption).foregroundStyle(Theme.amber)
                        .accessibilityAddTraits(.isStaticText)
                }
                SilverButton(title: "Give it to Bruce", icon: "arrow.right") { submitText(isLink: isLink) }
                    .opacity(currentText(isLink).isEmpty ? 0.5 : 1)
                    .disabled(currentText(isLink).isEmpty)
            }
            .padding(.horizontal, 20).padding(.bottom, 24)
        }
        .scrollDismissesKeyboard(.interactively)
    }

    private func currentText(_ isLink: Bool) -> String {
        (isLink ? link : draft).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func submitText(isLink: Bool) {
        let value = currentText(isLink)
        guard !value.isEmpty else { return }
        if isLink, URL(string: value)?.scheme == nil {
            localError = "That doesn't look like a link. Try including https://"
            return
        }
        Haptics.tap()
        // A link is submitted as text today; server-side link fetching is a documented follow-up.
        session.submit(text: value, type: isLink ? .link : .text)
    }

    // MARK: photo / PDF

    private func triggerPhoto() {
        // Present the system photo picker by toggling a real PhotosPicker via a hidden anchor.
        photoPickerPresented = true
    }
    @State private var photoPickerPresented = false

    private var sendingState: some View {
        VStack(spacing: 16) {
            ProgressView().tint(Theme.text).scaleEffect(1.3).padding(.top, 30)
            Text(session.uploading ? "Uploading…" : "Handing it to Bruce…")
                .font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
            Text("This takes a moment — you'll see it start tracking right away.")
                .font(.subheadline).foregroundStyle(Theme.textSecondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity).padding(.horizontal, 30)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(session.uploading ? "Uploading" : "Handing it to Bruce")
    }

    private var problemState: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(session.stage == .sessionExpired ? "Session expired" : "Couldn't reach Bruce",
                  systemImage: session.stage == .sessionExpired ? "person.crop.circle.badge.exclamationmark" : "wifi.exclamationmark")
                .font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
            if let r = session.blockingReason {
                Text(r).font(.subheadline).foregroundStyle(Theme.textSecondary)
                    .accessibilityAddTraits(.isStaticText)
            }
            SilverButton(title: "Try again", icon: "arrow.clockwise") { Haptics.tap(); session.retry() }
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 20)
    }

    // MARK: loaders

    @MainActor private func handlePhoto(_ item: PhotosPickerItem?) async {
        guard let item else { return }
        guard let data = try? await item.loadTransferable(type: Data.self),
              let img = UIImage(data: data), let png = img.pngData() else {
            localError = "Couldn't read that image. Try another."
            return
        }
        guard png.count < 15_000_000 else { localError = "That image is too large."; return }
        session.submit(bytes: png, mime: "image/png", sourceKind: "image", type: .photo)
    }

    private func handlePDF(_ result: Result<URL, Error>) {
        guard case .success(let url) = result else { return }
        let ok = url.startAccessingSecurityScopedResource()
        defer { if ok { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url), !data.isEmpty else {
            localError = "Couldn't open that PDF."; return
        }
        guard data.count < 15_000_000 else { localError = "That PDF is too large."; return }
        session.submit(bytes: data, mime: "application/pdf", sourceKind: "pdf", type: .pdf)
    }
}
