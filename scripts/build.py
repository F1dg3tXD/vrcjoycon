#!/usr/bin/env python3
"""Cross-platform PyInstaller build script for VRCJoyCon."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "main.py"
IMAGES = ROOT / "images"

if sys.platform.startswith("win"):
    icon = IMAGES / "app.ico"
else:
    icon = IMAGES / "app.png"

sep = os.pathsep
add_data = f"{IMAGES}{sep}images"

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name", "VRCJoyCon",
    "--icon", str(icon),
    "--add-data", add_data,
    str(SRC),
]

# Ensure src is importable during analysis
env = os.environ.copy()
env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

print("Running:", " ".join(cmd))
subprocess.run(cmd, cwd=ROOT, env=env, check=True)
print("Build complete. Output in", ROOT / "dist")
