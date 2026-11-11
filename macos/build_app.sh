#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_PATH="${PYTHON_PATH:-$(command -v python3)}"
APP_NAME="NFL EV Tracker"
APP_BUNDLE="/Applications/${APP_NAME}.app"

echo "Building ${APP_BUNDLE}..."
echo "  Project root : ${PROJECT_ROOT}"
echo "  Python       : ${PYTHON_PATH}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

ICONSET_DIR="${TMP_DIR}/AppIcon.iconset"
swift "${SCRIPT_DIR}/make_icon.swift" "${ICONSET_DIR}"
iconutil -c icns "${ICONSET_DIR}" -o "${TMP_DIR}/AppIcon.icns"

rm -rf "${APP_BUNDLE}"
mkdir -p "${APP_BUNDLE}/Contents/MacOS" "${APP_BUNDLE}/Contents/Resources"

cp "${TMP_DIR}/AppIcon.icns" "${APP_BUNDLE}/Contents/Resources/AppIcon.icns"

cat > "${APP_BUNDLE}/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.nfl.evtracker</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>${APP_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key>
        <true/>
    </dict>
    <key>NFLEVProjectRoot</key>
    <string>${PROJECT_ROOT}</string>
    <key>NFLEVPythonPath</key>
    <string>${PYTHON_PATH}</string>
</dict>
</plist>
EOF

swiftc \
    -O \
    -framework AppKit \
    -framework WebKit \
    "${SCRIPT_DIR}/NFLEVTrackerApp.swift" \
    -o "${APP_BUNDLE}/Contents/MacOS/${APP_NAME}"

codesign --force --deep --sign - "${APP_BUNDLE}"
echo "Built: ${APP_BUNDLE}"
