#!/bin/bash
# Creates a self-signed code-signing certificate "SingTogether Local Signing" in the login keychain:  tools/make-signing-cert.sh
#
# Why: an ad-hoc signed app changes identity with every build, so macOS asks for the Files and Folders
# permission (Desktop, Documents…) again after every rebuild or update. A signature from one stable
# certificate keeps the grant. The certificate carries no Apple trust, so Gatekeeper treats the app
# exactly like an ad-hoc one (allow once on first launch, see README). build.sh and dist.sh pick it up
# automatically when it exists. The .p12 (with password "singtogether") is kept in
# ~/Library/Application Support/SingTogether/signing/ for the release workflow (optional, see README).
set -euo pipefail
NAME="SingTogether Local Signing"
DIR="$HOME/Library/Application Support/SingTogether/signing"
KC="$HOME/Library/Keychains/login.keychain-db"
mkdir -p "$DIR" && chmod 700 "$DIR" && cd "$DIR"

if security find-identity -v -p codesigning 2>/dev/null | grep -q "\"$NAME\""; then
    echo "already there: $NAME"; exit 0
fi
/usr/bin/openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes -keyout key.pem -out cert.pem \
    -subj "/CN=$NAME/O=SingTogether" \
    -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=critical,codeSigning" -addext "basicConstraints=critical,CA:false"
/usr/bin/openssl pkcs12 -export -out signing.p12 -inkey key.pem -in cert.pem -name "$NAME" -passout pass:singtogether
rm -f key.pem
chmod 600 signing.p12
security import signing.p12 -k "$KC" -P singtogether -T /usr/bin/codesign -T /usr/bin/security -A
# trust it for code signing in your own account (macOS asks for your password once)
security add-trusted-cert -p codeSign -k "$KC" cert.pem
security find-identity -v -p codesigning | grep "$NAME" && echo "Done: $NAME (certificate and .p12 in $DIR)"
