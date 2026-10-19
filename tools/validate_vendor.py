#!/usr/bin/env python3
"""Validate an offline, pure-Python Venus dependency directory."""

import argparse
import os
from pathlib import Path
import sys


REQUIRED_PACKAGES = (
    "yaml",
    "pymodbus",
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
)
FORBIDDEN_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")


def validate_vendor(root: Path) -> list[str]:
    errors = []
    if not root.is_dir():
        return ["vendor directory does not exist: %s" % root]

    for package in REQUIRED_PACKAGES:
        marker = root / package / "__init__.py"
        if not marker.is_file():
            errors.append("missing Python package: %s" % package)

    for path in root.rglob("*"):
        if path.is_symlink():
            target = os.readlink(path)
            if os.path.isabs(target):
                errors.append("absolute symlink is not portable: %s -> %s" % (
                    path.relative_to(root), target
                ))
        if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(
                "compiled extension is not allowed in the portable bundle: %s"
                % path.relative_to(root)
            )
        if path.name == "__pycache__" or path.suffix in (".pyc", ".pyo"):
            errors.append("generated Python cache must be removed: %s" % (
                path.relative_to(root)
            ))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vendor", type=Path)
    args = parser.parse_args()
    errors = validate_vendor(args.vendor)
    if errors:
        for error in errors:
            print("ERROR: %s" % error, file=sys.stderr)
        return 1
    print("Vendor bundle: OK (%s)" % args.vendor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
