#!/usr/bin/env bash
# Gera o .app e o .dmg do Lelia Manager (macOS).
# Uso: manager/build-mac.sh   (a partir da raiz do repo)
set -euo pipefail
cd "$(dirname "$0")/.."

APP="Lelia Manager"
BIN="lelia_manager"

echo "==> PyInstaller (--windowed .app)"
.venv/bin/pyinstaller --noconfirm --clean \
    --windowed --name "$APP" \
    --icon manager/LeliaManager.icns \
    --osx-bundle-identifier com.lelia.manager \
    --add-data "manager/logo_64.png:." \
    manager/lelia_manager.py

APP_PATH="dist/$APP.app"
[ -d "$APP_PATH" ] || { echo "ERRO: $APP_PATH não gerado"; exit 1; }

echo "==> DMG"
DMG="dist/Lelia-Manager-$(date +%Y%m%d).dmg"
hdiutil create -volname "$APP" -srcfolder "$APP_PATH" \
    -ov -format UDZO "$DMG"

echo "Pronto: $DMG"
echo "Para distribuir sem aviso do Gatekeeper, assine/notarize o .app"
echo "(codesign --deep + xcrun notarytool) antes de gerar o DMG final."
