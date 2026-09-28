// swift-tools-version:5.9
// Aura menu-bar shell — thin native host for the Python orchestrator.
import PackageDescription

let package = Package(
    name: "AuraMenuBar",
    platforms: [
        .macOS(.v13)
    ],
    targets: [
        .executableTarget(
            name: "AuraMenuBar",
            path: "Sources/AuraMenuBar"
        )
    ]
)
