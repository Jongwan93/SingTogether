// swift-tools-version:5.9
// 빌드: ./build.sh (앱 + CLI)  ·  Xcode에서 열기: open Package.swift
import PackageDescription

let package = Package(
    name: "SingTogether",
    platforms: [.macOS(.v12)],
    targets: [
        // 이조 로직. 앱과 CLI가 같이 쓴다.
        .target(name: "Transpose", path: "Sources/Transpose"),

        // 메뉴바 앱
        .executableTarget(
            name: "SingTogether",
            dependencies: ["Transpose"],
            path: "Sources/App",
            linkerSettings: [.linkedFramework("AppKit")]
        ),

        // 터미널용: singtogether-transpose <악보 파일> <반음> [--out <출력 파일>]
        .executableTarget(
            name: "singtogether-transpose",
            dependencies: ["Transpose"],
            path: "Sources/TransposeCLI"
        ),
    ]
)
