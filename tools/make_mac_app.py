"""Build PlaneSite.app for Apple Silicon Macs (macOS 15+), from Linux or a Mac.

usage: python tools/make_mac_app.py <python dir> <out dir> [--sign <rcodesign>] [--zip]

<python dir> is python-build-standalone's aarch64-apple-darwin "install_only" python/ folder with
PlaneSite's libraries installed into lib/python3.12/site-packages (pip install --target ...
--platform macosx_15_0_arm64 --only-binary=:all:, the same versions as the Windows runtime).

The bundle mirrors the Windows install, so the updater works unchanged:
  PlaneSite.app/Contents/MacOS/PlaneSite      launcher (shell script)
  PlaneSite.app/Contents/Resources/python/    the runtime (+ planesite_runtime.txt)
  PlaneSite.app/Contents/Resources/app/       the app (replaced by in-app updates)
--sign: ad-hoc signature with rcodesign (apple-codesign) when no Developer ID is used yet.
--zip: PlaneSite-<version>-mac-arm64.zip next to the app (symlinks and permissions kept).
"""
import importlib.util
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
BUNDLE_ID = "com.edkollc.planesite"
MIN_MACOS = "15.0"        # rasterio's macOS wheels need Sequoia; every Apple Silicon Mac can run it

LAUNCHER = """#!/bin/bash
# PlaneSite launcher: runs the bundled Python with the app. The window is a Chrome / Edge app
# window (or a tab in the default browser); this process is PlaneSite's local server.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
export PLANESITE_APP_BUNDLE=1
# keep Python's compiled-file cache out of the app bundle
export PYTHONPYCACHEPREFIX="$HOME/Library/Caches/PlaneSite/pycache"
# once macOS has let the app open, clear the download mark from the files inside it too
xattr -dr com.apple.quarantine "$RES/../.." 2>/dev/null
cd "$RES"
exec "$RES/python/bin/python3" "$RES/app/main.py" "$@"
"""

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>PlaneSite</string>
  <key>CFBundleDisplayName</key><string>PlaneSite</string>
  <key>CFBundleIdentifier</key><string>{bid}</string>
  <key>CFBundleExecutable</key><string>PlaneSite</string>
  <key>CFBundleIconFile</key><string>PlaneSite</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>{ver}</string>
  <key>CFBundleVersion</key><string>{ver}</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>{minos}</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <!-- no Dock icon of its own: the window is a browser app window -->
  <key>LSUIElement</key><true/>
  <key>NSHumanReadableCopyright</key><string>Edko LLC</string>
</dict>
</plist>
"""


def version():
    spec = importlib.util.spec_from_file_location("ps", os.path.join(APP, "planesite", "__init__.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.__version__, m.RUNTIME


def main():
    args = sys.argv[1:]
    pydir, out = args[0], args[1]
    sign = args[args.index("--sign") + 1] if "--sign" in args else None
    ver, runtime = version()
    app = os.path.join(out, "PlaneSite.app")
    if os.path.exists(app):
        shutil.rmtree(app)
    res = os.path.join(app, "Contents", "Resources")
    macos = os.path.join(app, "Contents", "MacOS")
    os.makedirs(res)
    os.makedirs(macos)
    with open(os.path.join(app, "Contents", "Info.plist"), "w") as f:
        f.write(PLIST.format(bid=BUNDLE_ID, ver=ver, minos=MIN_MACOS))
    with open(os.path.join(app, "Contents", "PkgInfo"), "w") as f:
        f.write("APPL????")
    lp = os.path.join(macos, "PlaneSite")
    with open(lp, "w") as f:
        f.write(LAUNCHER)
    os.chmod(lp, 0o755)
    icns = os.path.join(out, "PlaneSite.icns")
    if os.path.exists(icns):
        shutil.copy(icns, os.path.join(res, "PlaneSite.icns"))
    # the runtime, without its test suite and what PlaneSite doesn't use on a Mac
    skip = {"test", "idlelib", "turtledemo", "ensurepip", "__pycache__"}
    shutil.copytree(pydir, os.path.join(res, "python"), symlinks=True,
                    ignore=lambda d, names: [n for n in names if n in skip])
    with open(os.path.join(res, "python", "planesite_runtime.txt"), "w") as f:
        f.write(str(runtime))
    # the app (as an in-app update would install it), without Windows-only files
    shutil.copytree(APP, os.path.join(res, "app"), symlinks=True,
                    ignore=lambda d, names: [n for n in names if n == "__pycache__"
                                             or n.endswith((".pyc", ".dll"))])
    if sign:
        subprocess.run([sign, "sign", app], check=True)
    if "--zip" in args:
        z = os.path.join(out, "PlaneSite-%s-mac-arm64.zip" % ver)
        if os.path.exists(z):
            os.remove(z)
        subprocess.run(["zip", "-qry", z, "PlaneSite.app"], cwd=out, check=True)
        print("zip:", z, os.path.getsize(z) // 2 ** 20, "MB")
    print("built", app, "version", ver)


if __name__ == "__main__":
    main()
