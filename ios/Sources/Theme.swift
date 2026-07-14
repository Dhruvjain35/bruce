import SwiftUI

extension Color {
    init(hex: UInt, alpha: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: alpha
        )
    }
}

/// Dark + metallic-silver design system. No purple, no default web fonts — SF Pro, deliberate.
enum Theme {
    static let bg = Color(hex: 0x07070A)
    static let surface = Color(hex: 0x15151B)
    static let surfaceHi = Color(hex: 0x20202A)
    static let stroke = Color.white.opacity(0.06)
    static let strokeHi = Color.white.opacity(0.16)

    static let text = Color.white.opacity(0.96)
    static let textSecondary = Color.white.opacity(0.56)
    static let textTertiary = Color.white.opacity(0.34)

    // Semantic accents — color is used ONLY to carry these four meanings.
    static let amber = Color(hex: 0xF5C451)   // a decision is required soon
    static let green = Color(hex: 0x4ED17F)   // externally verified completion
    static let red = Color(hex: 0xFF6B6B)     // an actual failure / destructive

    /// Brushed-silver gradient for accents (active pill, primary action, key numbers).
    static let silver = LinearGradient(
        colors: [Color.white, Color(hex: 0xDADCE4), Color(hex: 0x9EA1AE)],
        startPoint: .top, endPoint: .bottom
    )
    /// Soft top-lit hairline — reads as a glass edge, not a drawn outline.
    static let silverEdge = LinearGradient(
        colors: [Color.white.opacity(0.16), Color.white.opacity(0.03)],
        startPoint: .top, endPoint: .bottom
    )
    /// Top-lit card fill for depth (not a flat color).
    static let cardFill = LinearGradient(
        colors: [Color(hex: 0x1E1F28), Color(hex: 0x111117)],
        startPoint: .top, endPoint: .bottom
    )
    /// Fade scrolling content into the background behind a bottom action bar — no gray material band.
    static let bottomFade = LinearGradient(
        colors: [bg.opacity(0), bg, bg],
        startPoint: .top, endPoint: .bottom
    )

    /// Ambient dark background with a cool silver/graphite glow (replaces the purple gradient).
    struct Backdrop: View {
        var body: some View {
            ZStack {
                bg
                RadialGradient(colors: [Color(hex: 0x5C6175).opacity(0.72), Color(hex: 0x2B2D39).opacity(0.18), .clear],
                               center: .init(x: 0.92, y: 0.0), startRadius: 4, endRadius: 580)
                RadialGradient(colors: [Color(hex: 0x3A3D4C).opacity(0.45), .clear],
                               center: .init(x: 0.02, y: 0.46), startRadius: 4, endRadius: 480)
                RadialGradient(colors: [Color(hex: 0x14151C).opacity(0.9), .clear],
                               center: .init(x: 0.5, y: 1.0), startRadius: 6, endRadius: 420)
            }
            .ignoresSafeArea()
        }
    }
}

/// Genuine glass surface — top-lit gradient fill, soft edge, and depth. The base material for the whole UI.
struct GlassBox: ViewModifier {
    var radius: CGFloat = 20
    func body(content: Content) -> some View {
        content
            .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(Theme.silverEdge, lineWidth: 1)
            )
            .shadow(color: .black.opacity(0.30), radius: 16, x: 0, y: 9)
    }
}
extension View {
    /// Wrap any view in the standard glass surface.
    func glass(_ radius: CGFloat = 20) -> some View { modifier(GlassBox(radius: radius)) }
}

/// Rounded card surface — convenience wrapper around `.glass()` with padding.
struct GlassCard<Content: View>: View {
    var padding: CGFloat = 16
    var radius: CGFloat = 22
    @ViewBuilder var content: Content
    var body: some View {
        content.padding(padding).glass(radius)
    }
}
