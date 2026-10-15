// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Transcriber",
    platforms: [.macOS("14.2")],
    targets: [
        .target(name: "TranscriberCore", path: "Sources/TranscriberCore"),
        .executableTarget(name: "Transcriber", dependencies: ["TranscriberCore"], path: "Sources/Transcriber"),
        // Fixtures/ — куски настоящих AX-дампов, тесты читают их с диска по #filePath,
        // сборке они не нужны.
        .testTarget(name: "TranscriberCoreTests", dependencies: ["TranscriberCore", "Transcriber"],
                    path: "Tests/TranscriberCoreTests", exclude: ["Fixtures"]),
    ]
)
