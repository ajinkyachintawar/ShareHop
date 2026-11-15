#!/bin/bash
# Build dist-universal/ShareHop-<version>-universal.zip: ShareHop.app for Apple Silicon + Intel, macOS 12.3+.
# Separate from build.sh: own work folder (build-universal/), own output (dist-universal/), own Python.
# Python is python.org's universal2 framework build, unpacked from the official installer without
# installing it (no admin rights needed) and relocated into build-universal/. Run from anywhere.
set -euo pipefail
cd "$(dirname "$0")"
VERSION=1.0.0
PYVER=3.14.10   # 3.14: pyobjc 13.0's cp313 CoreMedia wheel has a broken x86_64 slice (@rpath/libswiftCoreMedia.dylib, no LC_RPATH)
PKG=python-$PYVER-macos11.pkg
PKG_SHA256=286c7018aa2730f6747ca433dccdc2b31be45f078a9ea0ffa44d5f06368fd2d7   # from python.org's release page
MIN_MACOS=12.3   # ScreenCaptureKit; Python (11.0) and pyobjc wheels (10.13) are older
W=build-universal
OUT=dist-universal
APP=$OUT/ShareHop.app
ZIP=$OUT/ShareHop-$VERSION-universal.zip
START=$SECONDS

rm -rf "$W" "$OUT"
mkdir -p "$W/download" "$OUT"

# 1. Official installer, verified: pinned SHA-256 and the PSF Developer ID signature.
curl -fsSL -o "$W/download/$PKG" "https://www.python.org/ftp/python/$PYVER/$PKG"
echo "$PKG_SHA256  $W/download/$PKG" | shasum -a 256 -c -
pkgutil --check-signature "$W/download/$PKG" | grep >/dev/null "Developer ID Installer: Python Software Foundation (BMM5U3QVKW)"

# 2. Unpack only the framework payload, no installer run.
pkgutil --expand-full "$W/download/$PKG" "$W/pkg"
mv "$W/pkg/Python_Framework.pkg/Payload" "$W/Python.framework"
rm -rf "$W/pkg" "$W/download"

# 3. Relocate: rewrite every /Library/Frameworks/Python.framework install name. Absolute paths to the
# new place do not fit the binaries' header padding, so dependencies become @loader_path-relative and
# ids become @rpath/... (both always shorter than the original). Then re-sign each changed binary ad hoc.
OLD=/Library/Frameworks/Python.framework
find "$W/Python.framework" -type f | while read -r f; do
    file -b "$f" | grep -q Mach-O || continue
    args=()
    id=$(otool -D "$f" 2>/dev/null | sed -n 2p)
    case "$id" in $OLD/*) args+=(-id "@rpath/Python.framework${id#"$OLD"}") ;; esac
    for d in $(otool -L "$f" 2>/dev/null | awk '/^\t/{print $1}' | grep "^$OLD/" | sort -u); do
        [ "$d" = "$id" ] && continue
        rel=$(/usr/bin/python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' \
              "$W/Python.framework${d#"$OLD"}" "$(dirname "$f")")
        args+=(-change "$d" "@loader_path/$rel")
    done
    [ ${#args[@]} -gt 0 ] || continue
    install_name_tool "${args[@]}" "$f" 2>&1 | { grep -v 'invalidate the code signature' || true; }
    codesign -f -s - "$f" 2>&1 | { grep -v 'replacing existing signature' || true; }
done
PYBIN=$W/Python.framework/Versions/${PYVER%.*}/bin/python${PYVER%.*}
for a in arm64 x86_64; do arch -$a "$PYBIN" -c 'import platform, ssl; print("relocated python ok:", platform.machine())'; done

# 4. Build venv with pinned versions; universal2 wheels only (never a host-only source build).
"$PYBIN" -m venv "$W/venv"
PY=$W/venv/bin/python
"$PY" -m pip install -q --disable-pip-version-check --only-binary :all: py2app==0.28.10 setuptools==84.2.1 \
    pyobjc-core==13.0 pyobjc-framework-Cocoa==13.0 pyobjc-framework-Quartz==13.0 pyobjc-framework-CoreMedia==13.0 \
    pyobjc-framework-libdispatch==13.0 pyobjc-framework-ScreenCaptureKit==13.0

# 5. Icon: build.sh's own drawing code (its first heredoc), run inside the work folder so it writes there.
awk '/<<.EOF.$/{f=1; next} f && /^EOF$/{exit} f' build.sh > "$W/icon.py"
(cd "$W" && venv/bin/python icon.py && iconutil -c icns build/ShareHop.iconset -o ShareHop.icns)

# 6. py2app (arch defaults to the Python's: universal2).
"$PY" - py2app --dist-dir "$OUT" --bdist-base "$W/py2app" <<EOF
from setuptools import setup
setup(name="ShareHop", app=["sharehop.py"], options={"py2app": {
    "iconfile": "$W/ShareHop.icns",
    # tkinter: python.org's Python bundles Tcl/Tk (Homebrew's does not); sharehop.py never uses it (+50 MB).
    "excludes": ["setuptools", "pkg_resources", "tkinter", "_tkinter"],
    "plist": {
        "CFBundleIdentifier": "io.github.sharehop.ShareHop",
        "CFBundleName": "ShareHop",
        "CFBundleDisplayName": "ShareHop",
        "CFBundleShortVersionString": "$VERSION",
        "CFBundleVersion": "$VERSION",
        "LSUIElement": True,
        "LSMinimumSystemVersion": "$MIN_MACOS",
        "NSScreenCaptureUsageDescription": "ShareHop records your screen to mirror the display under your mouse into its shareable window.",
    },
}})
EOF
# py2app records the build machine's Python path in the plist. The app does not need it, and it
# would publish the builder's home folder name, so drop it before signing.
/usr/libexec/PlistBuddy -c "Delete :PythonInfoDict:PythonExecutable" "$APP/Contents/Info.plist"
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"

# 7. Checks: every binary universal, no paths outside the bundle/system.
bad=0
while read -r f; do
    file -b "$f" | grep -q Mach-O || continue
    archs=$(lipo -archs "$f")
    [[ " $archs " == *" arm64 "* && " $archs " == *" x86_64 "* ]] || { echo "NOT UNIVERSAL ($archs): $f"; bad=1; }
    otool -L "$f" 2>/dev/null | awk '/^\t/{print $1}' | grep -E '^(/opt/homebrew|/usr/local|/Library/Frameworks|/Users/)|venv' \
        && { echo "  ^ in $f"; bad=1; }
done < <(find "$APP" -type f)
[ $bad = 0 ] || { echo "bundle checks failed"; exit 1; }

# 8. Self-test natively and under Rosetta. Frozen, the app logs to ~/Library/Logs/ShareHop.log
# (truncating it), so keep the installed copy's log and put it back afterwards.
LOG=~/Library/Logs/ShareHop.log
[ -f "$LOG" ] && cp -p "$LOG" "$W/ShareHop.log.keep"
for a in arm64 x86_64; do
    arch -$a "$APP/Contents/MacOS/ShareHop" --selftest || { echo "selftest failed ($a):"; cat "$LOG"; exit 1; }
    grep "selftest OK" "$LOG" >/dev/null || { echo "no 'selftest OK' in log ($a)"; exit 1; }
    echo "selftest OK ($a)"
done
if [ -f "$W/ShareHop.log.keep" ]; then mv "$W/ShareHop.log.keep" "$LOG"; else rm -f "$LOG"; fi

# 9. Zip, then remove the unpacked app and work folder (stray app copies confuse Spotlight launches).
ditto -c -k --keepParent "$APP" "$ZIP"
rm -rf "$APP" "$W"
echo "built $ZIP ($(du -h "$ZIP" | cut -f1), sha256 $(shasum -a 256 "$ZIP" | cut -d' ' -f1)) in $((SECONDS - START)) s"
