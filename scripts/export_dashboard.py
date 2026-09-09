#!/usr/bin/env python3
"""Export dashboard static JSON payloads.

Wrapper around export_static.py to maintain compatibility with workflows and VM scripts.
Exports to public/data and synchronizes dashboard/public/data.
"""
import shutil
import sys
from pathlib import Path

# Ensure repo root is on sys.path
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.export_static import main as export_main

if __name__ == "__main__":
    code = export_main()
    # If dashboard/public/data exists, keep it in sync
    src_data = REPO / "public" / "data"
    dash_data = REPO / "dashboard" / "public" / "data"
    if src_data.exists() and dash_data.parent.exists():
        dash_data.mkdir(parents=True, exist_ok=True)
        for f in src_data.glob("*.json"):
            shutil.copy2(f, dash_data / f.name)
    sys.exit(code)
