import SwiftUI

/// Aura's face: a native, GPU-drawn orb that reflects what the engine is doing.
///
/// No canvas, no web view — just SwiftUI shapes and animation, so it stays
/// crisp at any scale and costs nothing when the panel is closed.
struct OrbView: View {
    let phase: String
    var diameter: CGFloat = 132
    var action: () -> Void = {}

    @State private var breathe = false

    private var color: Color { Theme.color(for: phase) }

    private var isActive: Bool {
        ["capturing", "transcribing", "planning", "executing", "responding", "proposing"].contains(phase)
    }

    private var symbol: String {
        switch phase {
        case "capturing": return "waveform"
        case "transcribing", "planning": return "ellipsis"
        case "executing": return "gearshape.2.fill"
        case "responding": return "bubble.left.fill"
        case "proposing": return "shield.lefthalf.filled"
        case "disabled": return "power"
        default: return "mic.fill"
        }
    }

    var body: some View {
        Button(action: action) {
            ZStack {
                Circle()
                    .fill(
                        RadialGradient(colors: [color.opacity(0.55), color.opacity(0.12)],
                                       center: .center, startRadius: 2, endRadius: diameter * 0.62)
                    )
                    .blur(radius: 8)
                    .scaleEffect(breathe ? 1.06 : 0.94)

                Circle()
                    .fill(color.opacity(0.22))
                    .scaleEffect(breathe ? 1.12 : 1.0)
                    .opacity(isActive ? 1 : 0.45)

                Circle()
                    .fill(
                        LinearGradient(colors: [color.opacity(0.95), color.opacity(0.65)],
                                       startPoint: .topLeading, endPoint: .bottomTrailing)
                    )
                    .frame(width: diameter * 0.66, height: diameter * 0.66)
                    .shadow(color: color.opacity(0.45), radius: 12, y: 4)
                    .overlay(
                        Image(systemName: symbol)
                            .font(.system(size: diameter * 0.24, weight: .semibold))
                            .foregroundStyle(.white)
                    )
            }
            .frame(width: diameter, height: diameter)
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .help(helpText)
        .onAppear {
            withAnimation(.easeInOut(duration: isActive ? 0.9 : 2.6).repeatForever(autoreverses: true)) {
                breathe = true
            }
        }
    }

    private var helpText: String {
        switch phase {
        case "capturing": return "Aura is listening — say what you need"
        case "proposing": return "Aura is waiting for your go-ahead"
        default: return "Wake Aura (or press the menu-bar shortcut)"
        }
    }
}
