"""Patch the locked dmg-builder 22.14.13 vendor for Python 3, fail closed."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path


PYTHON_HOOK = 'process.env.PYTHON_PATH || "/usr/bin/python"'
OLD_RELOAD = "reload(sys)  # Reload is a hack"
NEW_RELOAD = "import importlib; importlib.reload(sys)  # Reload is a hack"
OLD_ENCODING = "sys.setdefaultencoding('UTF8')"
NEW_ENCODING = "pass  # Python 3 already uses UTF-8 for this build environment"


def patch_vendor(package: Path) -> Path:
    dmg_js = package / "out/dmg.js"
    core = package / "vendor/dmgbuild/core.py"
    if not dmg_js.is_file() or not core.is_file():
        raise RuntimeError("Locked dmg-builder files are missing")
    if dmg_js.read_text(encoding="utf-8").count(PYTHON_HOOK) != 1:
        raise RuntimeError("dmg-builder PYTHON_PATH entry point changed")
    source = core.read_text(encoding="utf-8")
    if source.count(OLD_RELOAD) == 1 and source.count(OLD_ENCODING) == 1:
        source = source.replace(OLD_RELOAD, NEW_RELOAD).replace(OLD_ENCODING, NEW_ENCODING)
        ast.parse(source, filename=str(core))
        core.write_text(source, encoding="utf-8")
    elif source.count(NEW_RELOAD) != 1 or source.count(NEW_ENCODING) != 1:
        raise RuntimeError("Vendored dmgbuild Python source changed")
    return core


def smoke(core: Path) -> None:
    """Exercise vendored imports and Finder metadata writing, without an image build."""
    with tempfile.TemporaryDirectory(prefix="e7-dmg-python3-") as directory:
        env = os.environ.copy()
        env.update(iconTextSize="12", iconSize="80", volumePath=directory,
                   iconLocations="'Smoke.app': (130, 220),", backgroundColor="#ffffff")
        subprocess.run([sys.executable, str(core)], cwd=core.parents[1], env=env,
                       check=True, timeout=30)
        if not (Path(directory) / ".DS_Store").is_file():
            raise RuntimeError("Vendored dmgbuild did not write Finder metadata")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: patch-dmg-python3.py PATH_TO_DMG_BUILDER")
    smoke(patch_vendor(Path(sys.argv[1]).resolve()))
