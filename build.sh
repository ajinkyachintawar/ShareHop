#!/bin/bash
# Build dist/ShareHop.app (py2app, standalone, ad-hoc signed). Run from anywhere: ./build.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
[ -x "$PY" ] || python3.14 -m venv .venv
"$PY" -m pip install -q -r requirements.txt py2app==0.28.10

rm -rf build dist
mkdir -p build

# App icon: two overlapping rounded screens with an arrow, drawn with AppKit, packed by iconutil.
"$PY" - <<'EOF'
import os, AppKit
from AppKit import NSBezierPath, NSColor, NSImage, NSMakeRect, NSBitmapImageRep
os.makedirs("build/ShareHop.iconset", exist_ok=True)
def draw(px):
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    s = px / 1024
    R = lambda x, y, w, h: NSMakeRect(x * s, y * s, w * s, h * s)
    NSColor.colorWithSRGBRed_green_blue_alpha_(0.13, 0.15, 0.22, 1).setFill()          # tile
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(R(100, 100, 824, 824), 185 * s, 185 * s).fill()
    for (x, y), c in (((180, 400), (0.35, 0.62, 1.0)), ((400, 240), (0.30, 0.85, 0.65))):
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.13, 0.15, 0.22, 1).setFill()      # gap around each screen
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(R(x - 22, y - 22, 484, 364), 62 * s, 62 * s).fill()
        NSColor.colorWithSRGBRed_green_blue_alpha_(*c, 1).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(R(x, y, 440, 320), 40 * s, 40 * s).fill()
    NSColor.whiteColor().set()                                                           # hop arrow
    p = NSBezierPath.bezierPath()
    p.setLineWidth_(46 * s); p.setLineCapStyle_(AppKit.NSLineCapStyleRound); p.setLineJoinStyle_(AppKit.NSLineJoinStyleRound)
    p.moveToPoint_((330 * s, 560 * s)); p.curveToPoint_controlPoint1_controlPoint2_((620 * s, 420 * s), (380 * s, 420 * s), (480 * s, 400 * s))
    p.stroke()
    h = NSBezierPath.bezierPath()
    h.setLineWidth_(46 * s); h.setLineCapStyle_(AppKit.NSLineCapStyleRound); h.setLineJoinStyle_(AppKit.NSLineJoinStyleRound)
    h.moveToPoint_((540 * s, 495 * s)); h.lineToPoint_((620 * s, 420 * s)); h.lineToPoint_((525 * s, 375 * s))
    h.stroke()
    AppKit.NSGraphicsContext.restoreGraphicsState()
    return rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
for pt in (16, 32, 128, 256, 512):
    for k in (1, 2):
        draw(pt * k).writeToFile_atomically_(f"build/ShareHop.iconset/icon_{pt}x{pt}{'@2x' if k == 2 else ''}.png", True)
EOF
iconutil -c icns build/ShareHop.iconset -o build/ShareHop.icns

VERSION=1.0.0
"$PY" - py2app <<EOF
from setuptools import setup
setup(name="ShareHop", app=["sharehop.py"], options={"py2app": {
    "iconfile": "build/ShareHop.icns",
    "excludes": ["setuptools", "pkg_resources"],   # build-time only; sharehop.py never imports them
    "plist": {
        "CFBundleIdentifier": "io.github.sharehop.ShareHop",
        "CFBundleName": "ShareHop",
        "CFBundleDisplayName": "ShareHop",
        "CFBundleShortVersionString": "$VERSION",
        "CFBundleVersion": "$VERSION",
        "LSUIElement": True,
        "LSMinimumSystemVersion": "26.0",
        "NSScreenCaptureUsageDescription": "ShareHop records your screen to mirror the display under your mouse into its shareable window.",    },
}})
EOF

# py2app records the build machine's Python path in the plist. The app does not need it, and it
# would publish the builder's home folder name, so drop it before signing.
/usr/libexec/PlistBuddy -c "Delete :PythonInfoDict:PythonExecutable" dist/ShareHop.app/Contents/Info.plist
codesign --force --deep -s - dist/ShareHop.app
codesign --verify --deep --strict dist/ShareHop.app
echo "built dist/ShareHop.app ($(du -sh dist/ShareHop.app | cut -f1))"
