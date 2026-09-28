import AuraCore
import SwiftUI

/// Aura's small design system — one place for colour, spacing and the three
/// container shapes the whole app is built from, so every screen looks like it
/// belongs to the same product.
enum Theme {

    // State colours (mirroring the menu-bar icon)
    static let ready = Color(red: 0.24, green: 0.80, blue: 0.55)
    static let busy = Color(red: 0.16, green: 0.55, blue: 1.00)
    static let attention = Color(red: 1.00, green: 0.62, blue: 0.12)
    static let down = Color(red: 0.96, green: 0.35, blue: 0.34)
    static let danger = Color(red: 0.93, green: 0.29, blue: 0.29)

    static let cardRadius: CGFloat = 14
    static let panelWidth: CGFloat = 400
    static let panelHeight: CGFloat = 620

    /// The colour of the orb / menu-bar icon for a given app phase.
    static func color(for phase: String) -> Color {
        switch phase {
        case "capturing", "transcribing", "planning", "executing", "responding": return busy
        case "proposing": return attention
        case "disabled": return .secondary
        default: return ready
        }
    }

    static func color(forLevel level: String) -> Color {
        switch level {
        case "ok", "granted", "safe": return ready
        case "confirm", "warning", "needs": return attention
        case "blocked", "denied", "failed", "destructive": return danger
        default: return .secondary
        }
    }
}

// MARK: - containers

/// The card: soft fill, hairline border, generous padding.
struct Card<Content: View>: View {
    var spacing: CGFloat = 10
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: spacing, content: content)
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                    .fill(Color.primary.opacity(0.05))
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                    .stroke(Color.primary.opacity(0.08), lineWidth: 1)
            )
    }
}

struct Badge: View {
    let text: String
    var color: Color = .secondary
    var icon: String?

    var body: some View {
        HStack(spacing: 4) {
            if let icon { Image(systemName: icon).font(.system(size: 9, weight: .bold)) }
            Text(text)
                .font(.system(size: 10, weight: .semibold))
                .textCase(.uppercase)
                .tracking(0.3)
        }
        .foregroundStyle(color)
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(Capsule().fill(color.opacity(0.14)))
    }
}

struct StatusDot: View {
    let color: Color
    var size: CGFloat = 8
    var pulsing = false
    @State private var animate = false

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
            .overlay(
                Circle()
                    .stroke(color.opacity(animate ? 0 : 0.5), lineWidth: 2)
                    .scaleEffect(animate ? 2.4 : 1)
                    .opacity(pulsing ? 1 : 0)
            )
            .onAppear {
                guard pulsing else { return }
                withAnimation(.easeOut(duration: 1.4).repeatForever(autoreverses: false)) {
                    animate = true
                }
            }
    }
}

/// A label/value row used across Settings and About.
struct InfoRow: View {
    let label: String
    let value: String
    var mono = false
    var color: Color?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label)
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.system(size: 12, weight: .medium, design: mono ? .monospaced : .default))
                .foregroundStyle(color ?? .primary)
                .multilineTextAlignment(.trailing)
                .textSelection(.enabled)
        }
    }
}

struct SectionTitle: View {
    let text: String
    var subtitle: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(text)
                .font(.system(size: 13, weight: .semibold))
            if let subtitle {
                Text(subtitle)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// The one button style used for "do the thing" actions.
struct AuraButtonStyle: ButtonStyle {
    var prominent = false
    var tint: Color = Theme.busy

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .semibold))
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(prominent ? tint.opacity(configuration.isPressed ? 0.75 : 1)
                                    : Color.primary.opacity(configuration.isPressed ? 0.14 : 0.07))
            )
            .foregroundStyle(prominent ? Color.white : Color.primary)
            .contentShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .opacity(configuration.isPressed ? 0.9 : 1)
    }
}

extension View {
    func auraButton(prominent: Bool = false, tint: Color = Theme.busy) -> some View {
        buttonStyle(AuraButtonStyle(prominent: prominent, tint: tint))
    }
}
