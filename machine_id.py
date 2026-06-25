#!/usr/bin/env python3
"""
stagewise Machine ID Tool

Read, reset, or spoof the Machine ID stored in identity.json.

Usage:
  python machine_id.py                    Show current Machine ID
  python machine_id.py --reset            Generate a new random Machine ID
  python machine_id.py --set <uuid>       Set a specific Machine ID
  python machine_id.py --read-appdata     Read from %APPDATA%/stagewise/stagewise/identity.json
"""

import json
import os
import sys
import uuid
from pathlib import Path


def get_identity_path():
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "stagewise" / "stagewise" / "identity.json"


def read_identity():
    p = get_identity_path()
    if not p.exists():
        return None, p
    try:
        return json.loads(p.read_text("utf-8")), p
    except Exception:
        return None, p


def write_identity(data, path=None):
    if path is None:
        path = get_identity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def main():
    identity, path = read_identity()

    print("stagewise Machine ID Tool")
    print("=" * 40)
    print(f"Path: {path}")
    print()

    if "--reset" in sys.argv:
        new_id = str(uuid.uuid4())
        data = {"machineId": new_id}
        write_identity(data, path)
        print(f"OK New Machine ID generated:")
        print(f"  {new_id}")
        if identity:
            print(f"  (was: {identity.get('machineId', 'N/A')})")
        return

    if "--set" in sys.argv:
        idx = sys.argv.index("--set")
        if idx + 1 >= len(sys.argv):
            print("Usage: python machine_id.py --set <uuid>")
            sys.exit(1)
        new_id = sys.argv[idx + 1]
        data = {"machineId": new_id}
        write_identity(data, path)
        print(f"OK Machine ID set to:")
        print(f"  {new_id}")
        return

    if not identity:
        print("X identity.json not found or unreadable")
        print("  The app will create one on first launch.")
        return

    print(f"Machine ID: {identity.get('machineId', 'N/A')}")
    for k, v in identity.items():
        if k != "machineId":
            print(f"  {k}: {v}")

    print()
    print("Options:")
    print("  --reset    Generate new random UUID")
    print("  --set ID   Set specific UUID")


if __name__ == "__main__":
    main()
