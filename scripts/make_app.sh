#!/usr/bin/env bash
# Assemble Aura.app from the Swift package — no Xcode project required.
#   ./scripts/make_app.sh            → build/Aura.app
#   ./scripts/make_app.sh --install  → /Applications/Aura.app
set -euo pipefail
cd "$(dirname "$0")/../macos"

say() { printf "\033[1;36m▸ %s\033[0m\n" "$1"; }
VERSION="0.5.1"

if ! command -v swift >/dev/null 2>&1; then
  echo "swift not found — install the Xcode command line tools first:"
  echo "  xcode-select --install"
  exit 1
fi

say "Building the Swift shell (swift build -c release)"
swift build -c release

APP="${1:-}"
INSTALL=false
[ "$APP" = "--install" ] && INSTALL=true
DEST="build/Aura.app"

say "Assembling $DEST"
# The icon is a generated artifact; regenerate it rather than fail the build.
if [ ! -f ../ui/icon.icns ]; then
  say "Icon missing — generating (needs Pillow: pip install pillow)"
  python3 ../scripts/make_icon.py
fi
rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"
cp .build/release/AuraMenuBar "$DEST/Contents/MacOS/Aura"
cp ../ui/icon.icns "$DEST/Contents/Resources/AppIcon.icns"

cat > "$DEST/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Aura</string>
    <key>CFBundleDisplayName</key><string>Aura</string>
    <key>CFBundleIdentifier</key><string>app.aura.menubar</string>
    <key>CFBundleExecutable</key><string>Aura</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>6</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSHumanReadableCopyright</key><string>© 2026 Aura Contributors. MIT.</string>
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
  xattr -dr com.apple.quarantine /Applications/Aura.app 2>/dev/null || true
  DEST="/Applications/Aura.app"
fi

say "Done — $DEST (v$VERSION)"
cat <<'EOF'

  Double-click Aura.app:
    · a ◉ appears in your menu bar
    · on first launch, a welcome window walks you through permissions
      (microphone, accessibility) — each is a normal macOS dialog
    · ⌥Space anywhere wakes Aura; right-click the ◉ for the menu

  Note: the ad-hoc signature runs fine locally. For "Start at Login" macOS
  may ask you to approve Aura under System Settings › General › Login Items.
EOF
