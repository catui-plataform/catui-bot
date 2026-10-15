"""Backward-compatible PCSX2 native Libretro probe."""
import argparse
from pathlib import Path

try:
    from validation.core_native import probe
except ImportError:
    from core_native import probe

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    probe("pcsx2", args.package, args.log)
