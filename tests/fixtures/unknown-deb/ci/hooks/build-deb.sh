#!/bin/sh
set -eu
package="$OUTPUT_DIR/fixture-unknown"
mkdir -p "$package/DEBIAN" "$package/usr/share/fixture-unknown"
printf 'Package: fixture-unknown\nVersion: %s-%s\nArchitecture: amd64\nMaintainer: Fixture <fixture@example.invalid>\nDescription: Unknown repository fixture\n' \
    "$PACKAGE_VERSION" "$PACKAGE_RELEASE" > "$package/DEBIAN/control"
cp "$SOURCE_DIR/payload.txt" "$package/usr/share/fixture-unknown/payload.txt"
dpkg-deb --build --root-owner-group "$package" "$OUTPUT_DIR/fixture-unknown_${PACKAGE_VERSION}-${PACKAGE_RELEASE}_amd64.deb"
