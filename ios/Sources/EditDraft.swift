import SwiftUI

/// Edit the drafted email before sending. Real, editable fields.
struct EditDraftView: View {
    let draft: DraftEmail
    @Environment(\.dismiss) private var dismiss
    @State private var subject: String
    @State private var bodyText: String

    init(draft: DraftEmail) {
        self.draft = draft
        _subject = State(initialValue: draft.subject)
        _bodyText = State(initialValue: draft.body)
    }

    var body: some View {
        ZStack {
            Theme.Backdrop()
            VStack(spacing: 0) {
                HStack {
                    Text("Edit email").font(.system(size: 18, weight: .bold)).foregroundStyle(Theme.text)
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.system(size: 14, weight: .bold)).foregroundStyle(Theme.textSecondary)
                            .frame(width: 32, height: 32).background(Theme.surfaceHi, in: Circle())
                    }.buttonStyle(.plain)
                }
                .padding(.horizontal, 18).padding(.top, 18).padding(.bottom, 12)

                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("TO").font(.system(size: 10, weight: .bold)).tracking(0.8).foregroundStyle(Theme.textTertiary)
                            Text(draft.to).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(14).glass(16)

                        VStack(alignment: .leading, spacing: 6) {
                            Text("SUBJECT").font(.system(size: 10, weight: .bold)).tracking(0.8).foregroundStyle(Theme.textTertiary)
                            TextField("", text: $subject, axis: .vertical)
                                .font(.system(size: 15)).foregroundStyle(Theme.text).tint(Theme.silver)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(14).glass(16)

                        VStack(alignment: .leading, spacing: 6) {
                            Text("BODY").font(.system(size: 10, weight: .bold)).tracking(0.8).foregroundStyle(Theme.textTertiary)
                            TextEditor(text: $bodyText)
                                .font(.system(size: 15)).foregroundStyle(Theme.text).tint(Theme.silver)
                                .scrollContentBackground(.hidden)
                                .frame(minHeight: 260)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(14).glass(16)

                        Color.clear.frame(height: 90)
                    }
                    .padding(.horizontal, 18)
                }
                .scrollIndicators(.hidden)
            }
        }
        .presentationDetents([.large])
        .preferredColorScheme(.dark)
        .safeAreaInset(edge: .bottom) {
            SilverButton(title: "Save changes", icon: "checkmark") { dismiss() }
                .padding(.horizontal, 18).padding(.bottom, 12).background(.ultraThinMaterial)
        }
    }
}
