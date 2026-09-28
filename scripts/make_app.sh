#!/usr/bin/env bash
# Assemble Aura.app from the Swift package — no Xcode project required.
#   ./scripts/make_app.sh            → build/Aura.app
#   ./scripts/make_app.sh --install  → /Applications/Aura.app
set -euo pipefail
cd "$(dirname "$0")/../macos"

say() { printf "\033[1;36m▸ %s\033[0m\n" "$1"; }

say "Building the Swift shell (swift build -c release)"
swift build -c release

APP="${1:-}"
INSTALL=false
[ "$APP" = "--install" ] && INSTALL=true
DEST="build/Aura.app"

say "Assembling $DEST"
rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"
cp .build/release/AuraMenuBar "$DEST/Contents/MacOS/Aura"

cat > "$DEST/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Aura</string>
    <key>CFBundleDisplayName</key><string>Aura</string>
    <key>CFBundleIdentifier</key><string>app.aura.menubar</string>
    <key>CFBundleExecutable</key><string>Aura</string>
    <key>CFBundleShortVersionString</key><string>0.4.0</string>
    <key>CFBundleVersion</key><string>3</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Aura uses the microphone only to hear your wake phrase and commands. Audio never leaves this Mac.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>Aura sends Apple events to drive the apps you ask it to control (e.g. Safari, Spotify, Finder).</string>
</dict>
</plist>
PLIST

say "Ad-hoc code signing"
codesign --force --sign - "$DEST" 2>/dev/null || true

if $INSTALL; then
  say "Installing to /Applications"
  rm -rf /Applications/Aura.app
  cp -R "$DEST" /Applications/Aura.app
  DEST="/Applications/Aura.app"
fi

say "Done — $DEST"
cat <<'EOF'

  Double-click Aura.app: a ◉ appears in your menu bar.
  First launch: pick your mac-assistant folder when asked.
  ⌥Space anywhere → wake Aura. Right-click the ◉ for the menu.

  Note: the ad-hoc signature runs fine locally. For "Start at Login" macOS
  may ask you to approve Aura under System Settings › General › Login Items.
EOF
