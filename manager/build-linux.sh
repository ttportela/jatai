#!/usr/bin/env bash
# Gera o binário Linux e o pacote .deb do Jataí Manager.
# Uso: manager/build-linux.sh   (a partir da raiz do repo)
set -euo pipefail
cd "$(dirname "$0")/.."

PKG="jatai-manager"
VER="0.1.0"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

echo "==> PyInstaller (--onefile)"
.venv/bin/pyinstaller --noconfirm --clean \
    --onefile --name "$PKG" \
    --icon manager/logo.png \
    --add-data "manager/logo_64.png:." \
    manager/jatai_manager.py

BIN="dist/$PKG"
[ -f "$BIN" ] || { echo "ERRO: $BIN não gerado"; exit 1; }

echo "==> Montando .deb"
DEBROOT="dist/debroot"
rm -rf "$DEBROOT"
mkdir -p "$DEBROOT/DEBIAN" "$DEBROOT/usr/bin" "$DEBROOT/usr/share/applications"

cp "$BIN" "$DEBROOT/usr/bin/$PKG"
chmod 755 "$DEBROOT/usr/bin/$PKG"
cp manager/jatai-manager.desktop "$DEBROOT/usr/share/applications/$PKG.desktop"
sed -i "s|Exec=jatai-manager|Exec=/usr/bin/$PKG|;s|Icon=jatai|Icon=jatai|" \
    "$DEBROOT/usr/share/applications/$PKG.desktop"
cp logo.png "$DEBROOT/usr/share/pixmaps/jatai.png" 2>/dev/null || true

cat > "$DEBROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VER
Section: science
Priority: optional
Architecture: $ARCH
Maintainer: Jataí <jatai@localhost>
Description: Gerenciador do Jataí Research Agent
 App desktop para instalar, configurar e executar o Jataí
 (pasta de trabalho, porta, iniciar/parar).
EOF

dpkg-deb --build "$DEBROOT" "dist/${PKG}_${VER}_${ARCH}.deb"
echo "Pronto: dist/${PKG}_${VER}_${ARCH}.deb"
