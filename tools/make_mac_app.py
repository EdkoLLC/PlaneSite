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
# Updates never change this bundle: they go to ~/Library/Application Support/PlaneSite/app,
# and choose_app.py picks that copy when it is newer than the one built in.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
export PLANESITE_APP_BUNDLE=1
export PLANESITE_BUNDLE_RES="$RES"
export PLANESITE_TURBOJPEG="$RES/app/planesite/bin/libturbojpeg.dylib"
# keep Python's compiled-file cache out of the app bundle
export PYTHONPYCACHEPREFIX="$HOME/Library/Caches/PlaneSite/pycache"
# once macOS has let the app open, clear the download mark from the files inside it too
xattr -dr com.apple.quarantine "$RES/../.." 2>/dev/null
PY="$RES/python/bin/python3"
APP="$("$PY" -I "$RES/choose_app.py" "$RES" 2>/dev/null)"
[ -f "$APP/main.py" ] || APP="$RES/app"
cd "$RES"
# Start PlaneSite in the background and return straight away. PlaneSite isn't a Cocoa app, so it
# never "checks in" with macOS; if this launcher kept running, Finder would wait for it and then
# report that PlaneSite "is not open anymore". main.py reuses a PlaneSite that is already running.
nohup "$PY" "$APP/main.py" "$@" >/dev/null 2>&1 </dev/null &
disown
exit 0
"""

CHOOSER = r'''"""Prints the app folder to run: the updated copy in Application Support when it is newer
than the one inside PlaneSite.app and works with this app's Python, else the built-in one."""
import os
import re
import sys

res = sys.argv[1]
builtin = os.path.join(res, "app")
user = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "PlaneSite", "app")


def info(d):
    try:
        with open(os.path.join(d, "planesite", "__init__.py"), encoding="utf-8") as f:
            s = f.read()
    except OSError:
        return None
    v = re.search(r'^__version__\s*=\s*"([^"]+)"', s, re.M)
    r = re.search(r"^RUNTIME\s*=\s*(\d+)", s, re.M)
    if not v or not os.path.isfile(os.path.join(d, "main.py")):
        return None
    nums = [int(x) for x in re.findall(r"\d+", v.group(1))[:3]]
    return tuple(nums + [0] * (3 - len(nums))), int(r.group(1)) if r else 0


try:
    with open(os.path.join(res, "python", "planesite_runtime.txt")) as f:
        runtime = int(f.read().strip())
except Exception:
    runtime = 0
b, u = info(builtin), info(user)
pick = builtin
if u and (not b or u[0] > b[0]) and u[1] <= runtime:
    pick = user
print(pick)
'''

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
    with open(os.path.join(res, "choose_app.py"), "w") as f:
        f.write(CHOOSER)
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
