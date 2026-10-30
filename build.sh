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
# 서명: 로그인 키체인에 "SingTogether Local Signing"(tools/make-signing-cert.sh)이 있으면 그걸로, 없으면 ad-hoc.
# 같은 인증서로 서명해야 macOS가 파일 접근 권한을 빌드마다 다시 묻지 않는다.
IDENTITY="${SINGTOGETHER_SIGN:-$(security find-identity -v -p codesigning 2>/dev/null | grep -o '"SingTogether Local Signing"' | head -1 | tr -d '"')}"
codesign --force --sign "${IDENTITY:--}" "$APP" >/dev/null
cp .build/release/singtogether-transpose build/
echo "Done: $APP, build/singtogether-transpose"
echo "Run: open $APP   (install: cp -R $APP /Applications/)"
