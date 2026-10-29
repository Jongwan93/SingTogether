#!/bin/bash
# Build a self-contained SingTogether.app and a .dmg for other Macs:  ./dist.sh
#
#   dist/SingTogether.app          app + relocatable Python 3.13 + recognizer (tools/ocr) + singtogether-transpose
#   dist/SingTogether-<version>.dmg
#
# Signing: ad-hoc by default (recipients must right-click → Open the first time).
# With an Apple Developer ID:   DEVELOPER_ID="Developer ID Application: Name (TEAMID)" NOTARY_PROFILE=<notarytool keychain profile> ./dist.sh
# Needs: Xcode Command Line Tools, uv (https://docs.astral.sh/uv/), Apple Silicon (the bundled Python is arm64).
set -euo pipefail
cd "$(dirname "$0")"

PYVER="${PYVER:-3.13}"
VERSION=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" Info.plist)
APP=dist/SingTogether.app
RES="$APP/Contents/Resources"

echo "▸ app"
./build.sh >/dev/null
rm -rf dist && mkdir -p dist
cp -R build/SingTogether.app "$APP"

echo "▸ python $PYVER (uv managed, relocatable)"
uv python install "$PYVER" >/dev/null 2>&1 || true
PYHOME=$(dirname "$(dirname "$(uv python find "$PYVER" --managed-python)")")
cp -R "$PYHOME" "$RES/python"
PY="$RES/python/bin/python$PYVER"
[ -x "$PY" ] || { echo "no python at $PY"; exit 1; }

echo "▸ recognizer and its packages"
uv pip install --python "$PY" --break-system-packages --quiet ./tools/ocr
SP=$(dirname "$("$PY" -c 'import singtogether_ocr, os; print(os.path.dirname(singtogether_ocr.__file__))')")

echo "▸ homr models (bundled so the first run needs no download)"
DEV=tools/ocr/.venv/lib/python$PYVER/site-packages/homr
for d in transformer segmentation; do
    [ -d "$DEV/$d" ] && cp "$DEV/$d"/*.onnx "$SP/homr/$d/" 2>/dev/null || true
done
HOMR_COREML_MODEL_CACHE_DIR=$(mktemp -d) "$PY" -I -m homr.main --init 2>&1 | grep -v "^$" | tail -3 || true
rm -rf "$SP"/homr/*/*.coreml_cache

# trim what the app never uses
rm -rf "$SP/PyObjCTest" "$SP"/*/tests "$SP"/*/test "$SP"/*/__pycache__ "$SP"/pip* "$SP"/setuptools*
STD="$RES/python/lib/python$PYVER"
rm -rf "$STD/test" "$STD/idlelib" "$STD/tkinter" "$STD/turtledemo" "$STD/ensurepip" "$STD/__phello__" "$RES/python/share"
find "$RES/python" -name "__pycache__" -type d -prune -exec rm -rf {} +
# precompile so Python never writes .pyc files into the signed bundle at run time (the launcher also passes -B)
"$PY" -I -B -m compileall -q -j0 "$RES/python/lib" >/dev/null 2>&1 || true

mkdir -p "$RES/ocr/bin"
cat > "$RES/ocr/bin/singtogether-ocr" <<EOF
#!/bin/sh
# launcher used by the app (SheetOCR.locate): bundled Python, isolated from the user's environment
HERE="\$(cd "\$(dirname "\$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1      # for the child processes it starts (homr)
export SINGTOGETHER_TRANSPOSE="\${SINGTOGETHER_TRANSPOSE:-\$HERE/../../../MacOS/singtogether-transpose}"   # when run by hand, not by the app
exec "\$HERE/../../python/bin/python$PYVER" -I -B -X utf8 -m singtogether_ocr.cli "\$@"
EOF
chmod +x "$RES/ocr/bin/singtogether-ocr"

echo "▸ sign"
ENTITLEMENTS=dist/entitlements.plist
cat > "$ENTITLEMENTS" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
    <key>com.apple.security.cs.disable-library-validation</key><true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
</dict></plist>
EOF
IDENTITY="${DEVELOPER_ID:--}"
SIGN=(codesign --force --sign "$IDENTITY" --timestamp --options runtime --entitlements "$ENTITLEMENTS")
[ "$IDENTITY" = "-" ] && SIGN=(codesign --force --sign -)
# every Mach-O inside the bundle first (dylibs, .so, python binaries), the app last
find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) | while read -r f; do
    if file "$f" | grep -q "Mach-O"; then "${SIGN[@]}" "$f" 2>/dev/null || true; fi
done
"${SIGN[@]}" "$APP/Contents/MacOS/singtogether-transpose"
"${SIGN[@]}" "$APP"
codesign --verify --deep --strict "$APP"

echo "▸ dmg"
DMG="dist/SingTogether-$VERSION.dmg"
STAGE=dist/stage
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "SingTogether" -srcfolder "$STAGE" -ov -format UDZO -quiet "$DMG"
rm -rf "$STAGE"
[ "$IDENTITY" != "-" ] && codesign --force --sign "$IDENTITY" --timestamp "$DMG"

if [ -n "${NOTARY_PROFILE:-}" ]; then
    echo "▸ notarize"
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP"
    xcrun stapler staple "$DMG"
fi

du -sh "$APP" "$DMG"
echo "Done: $DMG"
[ "$IDENTITY" = "-" ] && echo "Ad-hoc signed: recipients allow it once on first launch, see README Install (or set DEVELOPER_ID and NOTARY_PROFILE)."
exit 0
