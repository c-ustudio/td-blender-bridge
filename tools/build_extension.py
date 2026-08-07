"""Build the Blender extension zip from the single-file add-on.

Usage: python tools/build_extension.py  ->  dist/td_blender_bridge-<ver>.zip

The extension format (Blender 4.2+) wants a package with a manifest and no
bl_info; the legacy single file keeps its bl_info so it stays installable on
its own. This script strips bl_info and packages __init__.py + manifest.
Install the result via Preferences > Get Extensions > Install from Disk.
"""
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "blender" / "td_blender_bridge.py"
MANIFEST = ROOT / "blender" / "blender_manifest.toml"


def main():
    code = SRC.read_text(encoding="utf-8")
    stripped, n = re.subn(r"bl_info = \{.*?\n\}\n", "", code, count=1,
                          flags=re.S)
    if n != 1:
        raise SystemExit("bl_info block not found - packaging aborted")
    manifest = MANIFEST.read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', manifest, re.M).group(1)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"td_blender_bridge-{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("blender_manifest.toml", manifest)
        z.writestr("__init__.py", stripped)
    print(f"built {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
