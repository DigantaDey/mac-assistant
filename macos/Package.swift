// swift-tools-version:5.9
// Aura — a native macOS menu-bar app with a local Python engine behind it.
//
//   AuraCore     Foundation-only: models, the engine client, the supervisor.
//                No AppKit, no SwiftUI — so it compiles and unit-tests fast,
//                and CI can run its tests on every push.
//   AuraMenuBar  The product: AppKit + SwiftUI, LSUIElement menu-bar app.
import PackageDescription

let package = Package(
    name: "Aura",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "Aura", targets: ["AuraMenuBar"]),
        .library(name: "AuraCore", targets: ["AuraCore"]),
    ],
    targets: [
        .target(
            name: "AuraCore",
            path: "Sources/AuraCore"
        ),
        .executableTarget(
            name: "AuraMenuBar",
            dependencies: ["AuraCore"],
            path: "Sources/AuraMenuBar"
        ),
        .testTarget(
            name: "AuraCoreTests",
            dependencies: ["AuraCore"],
            path: "Tests/AuraCoreTests"
        ),
    ]
)
