#!/bin/bash
# Build wsjtx-tools_<ver>_all.deb on any Debian / Ubuntu / Raspberry Pi OS:
#   bash build-deb.sh            -> ./wsjtx-tools_<ver>_all.deb
set -e
cd "$(dirname "$0")"
VER=$(sed -n 's/^Version: //p' packaging/DEBIAN/control)
W=$(mktemp -d)
cp -a packaging "$W/pkg"
find "$W/pkg" -type f -exec sed -i 's/\r$//' {} +
mkdir -p "$W/pkg/usr/share/wsjtx-tools" "$W/pkg/usr/bin"
for s in hrdlog-upload decode-split; do
  sed 's/\r$//' "$s.py" > "$W/pkg/usr/share/wsjtx-tools/$s.py"
  chmod 755 "$W/pkg/usr/share/wsjtx-tools/$s.py"
  ln -s "../share/wsjtx-tools/$s.py" "$W/pkg/usr/bin/$s"
done
mkdir -p "$W/pkg/usr/share/doc/wsjtx-tools"
sed 's/\r$//' README.md > "$W/pkg/usr/share/doc/wsjtx-tools/README.md"
chmod 755 "$W/pkg/DEBIAN/postinst"
find "$W/pkg" -type d -exec chmod 755 {} +
find "$W/pkg/usr" -type f ! -name '*.py' -exec chmod 644 {} +
dpkg-deb --root-owner-group --build "$W/pkg" "wsjtx-tools_${VER}_all.deb"
rm -rf "$W"
