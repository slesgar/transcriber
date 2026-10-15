// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "wkspike",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/argmaxinc/WhisperKit", exact: "1.1.0"),
    ],
    targets: [
        .executableTarget(
            name: "wkspike",
            dependencies: [.product(name: "WhisperKit", package: "WhisperKit")],
            path: "Sources/wkspike"
        ),
    ]
)
