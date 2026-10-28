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
codesign --force --sign - "$APP" >/dev/null
cp .build/release/singtogether-transpose build/
echo "Done: $APP, build/singtogether-transpose"
echo "Run: open $APP   (install: cp -R $APP /Applications/)"
