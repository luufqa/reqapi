#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def data_files() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    return sorted(
        path
        for path in DATA_DIR.rglob("*")
        if path.is_file() and path.name != ".gitkeep"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove all REQAPI runtime data: users, sessions, collections, requests, tokens, tab sets, workspaces, env vars, and local keys.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Run without interactive confirmation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting anything.",
    )
    args = parser.parse_args()

    files = data_files()
    if not files:
        print(f"No runtime data found in {DATA_DIR}")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / ".gitkeep").touch(exist_ok=True)
        return 0

    print("REQAPI runtime data to remove:")
    for path in files:
        print(f"  - {path.relative_to(ROOT)}")

    if args.dry_run:
        return 0

    if not args.yes:
        print()
        print("Stop the REQAPI service before running this reset.")
        confirmation = input("Type RESET to delete all runtime data: ").strip()
        if confirmation != "RESET":
            print("Cancelled.")
            return 1

    shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / ".gitkeep").touch(exist_ok=True)
    print(f"Removed all runtime data from {DATA_DIR}")
    print("Start REQAPI again; it will create a fresh empty database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
