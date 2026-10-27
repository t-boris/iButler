"""Package the UI separately from the stable camera/microphone permission parent."""
from __future__ import annotations
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUNDLE_ID = "com.boris.butler-ui"
# Resources next to the icon: the offline Mermaid page used by the "Display" section.
RESOURCES = ("Butler.icns", "mermaid.min.js", "diagram.html", "diagram_templates.json", "drawing_example.svg")


def package_version() -> str:
    """The ambient version string, read from the package source without importing it."""
    text = (ROOT / "butler_ambient/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise RuntimeError("butler_ambient/__init__.py has no __version__")
    return match.group(1)


def build():
    target = ROOT / "build/Butler.app"
    contents = target / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    version = package_version()
    info = {"CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "Butler", "CFBundleDisplayName": "Butler",
            "CFBundleExecutable": "Butler", "CFBundlePackageType": "APPL", "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication", "CFBundleIconFile": "Butler.icns",
            "CFBundleShortVersionString": version, "CFBundleVersion": version}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    shutil.copy2(ROOT / "build/butler-panel", contents / "MacOS/Butler")
    (contents / "Resources").mkdir(exist_ok=True)
    for name in RESOURCES:
        shutil.copy2(ROOT / "butler_ambient/assets" / name, contents / "Resources" / name)
    assert os.access(contents / "MacOS/Butler", os.X_OK)
    assert hashlib.sha256((contents / "MacOS/Butler").read_bytes()).digest() == hashlib.sha256((ROOT / "build/butler-panel").read_bytes()).digest()
    print("PASS Butler.app packaging; separate UI identity; no permission-parent changes")
    return target


def install(source: Path, destination: Path):
    """Replace only this UI bundle, rolling back if replacement fails."""
    if destination.exists():
        if destination.is_symlink() or plistlib.loads((destination / "Contents/Info.plist").read_bytes()).get("CFBundleIdentifier") != BUNDLE_ID:
            raise RuntimeError("Destination belongs to another application")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".butler-ui-", dir=destination.parent))
    staged = temporary / "Butler.app"
    previous = temporary / "previous.app"
    try:
        shutil.copytree(source, staged)
        if destination.exists(): destination.rename(previous)
        try: staged.rename(destination)
        except BaseException:
            if previous.exists(): previous.rename(destination)
            raise
    finally: shutil.rmtree(temporary)
    # A direct service-launched executable does not register the bundle with
    # Finder/LaunchServices. Refresh only this application, never global caches.
    registrar = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")
    if registrar.is_file():
        subprocess.run([str(registrar), "-f", str(destination)], check=True, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return destination / "Contents/MacOS/Butler"


if __name__ == "__main__":
    build()
