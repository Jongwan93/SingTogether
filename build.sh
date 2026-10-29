#!/bin/bash
# Build SingTogether: ./build.sh  →  build/SingTogether.app, build/singtogether-transpose
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release
APP=build/SingTogether.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp .build/release/SingTogether "$APP/Contents/MacOS/SingTogether"
cp Info.plist "$APP/Contents/Info.plist"
mkdir -p "$APP/Contents/Resources"
cp Resources/AppIcon.icns "$APP/Contents/Resources/"
cp .build/release/singtogether-transpose "$APP/Contents/MacOS/"   # 인식 도구가 이조와 SVG→PDF에 쓴다
codesign --force --sign - "$APP" >/dev/null
cp .build/release/singtogether-transpose build/
echo "Done: $APP, build/singtogether-transpose"
echo "Run: open $APP   (install: cp -R $APP /Applications/)"
