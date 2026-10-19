#!/usr/bin/env python3
"""Build a reproducible pure-Python dependency directory for Venus OS."""

import argparse
import hashlib
import inspect
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Optional
from urllib.request import urlopen

try:
    from .validate_vendor import validate_vendor
except ImportError:
    from validate_vendor import validate_vendor


PYYAML_VERSION = "6.0.2"


def pinned_requirements(path: Path) -> list[str]:
    values = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise ValueError("dependency is not exactly pinned: %s" % line)
        values.append(line)
    if not values:
        raise ValueError("dependency lock is empty")
    return values


def extract_pure_pyyaml(source: Path, vendor: Path) -> None:
    """Copy only PyYAML's portable lib/yaml package from its source archive."""
    with tempfile.TemporaryDirectory() as directory:
        unpacked = Path(directory)
        with tarfile.open(source, "r:gz") as archive:
            for member in archive.getmembers():
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise RuntimeError("unsafe path in PyYAML source archive")
            if "filter" in inspect.signature(archive.extractall).parameters:
                archive.extractall(unpacked, filter="data")
            else:
                archive.extractall(unpacked)
        candidates = list(unpacked.glob("*/lib/yaml/__init__.py"))
        if len(candidates) != 1:
            raise RuntimeError("unexpected PyYAML source archive layout")
        yaml_source = candidates[0].parent
        yaml_target = vendor / "yaml"
        if yaml_target.exists():
            shutil.rmtree(yaml_target)
        shutil.copytree(yaml_source, yaml_target)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def download_pyyaml_source(destination: Path, wheelhouse: Optional[Path]) -> Path:
    """Obtain and verify the sdist without invoking its build backend."""
    if wheelhouse is not None:
        matches = list(wheelhouse.glob("[Pp][Yy][Yy][Aa][Mm][Ll]-%s.tar.gz" % PYYAML_VERSION))
        if len(matches) != 1:
            raise RuntimeError("wheelhouse must contain one PyYAML source archive")
        target = destination / matches[0].name
        shutil.copy2(matches[0], target)
        return target

    metadata_url = "https://pypi.org/pypi/PyYAML/%s/json" % PYYAML_VERSION
    with urlopen(metadata_url, timeout=30) as response:
        metadata = json.load(response)
    candidates = [
        item for item in metadata.get("urls", [])
        if item.get("packagetype") == "sdist"
        and item.get("filename", "").endswith(".tar.gz")
    ]
    if len(candidates) != 1:
        raise RuntimeError("PyPI did not return exactly one PyYAML source archive")
    source = candidates[0]
    expected = source.get("digests", {}).get("sha256")
    if not expected:
        raise RuntimeError("PyYAML source archive has no SHA-256 metadata")
    with urlopen(source["url"], timeout=60) as response:
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise RuntimeError("PyYAML source archive SHA-256 mismatch")
    target = destination / source["filename"]
    target.write_bytes(payload)
    return target


def build_vendor(output: Path, lock: Path, wheelhouse: Optional[Path]) -> None:
    requirements = pinned_requirements(lock)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as directory:
        downloads = Path(directory)
        common = [sys.executable, "-m", "pip"]
        binary_options = []
        if wheelhouse is not None:
            binary_options = ["--no-index", "--find-links", str(wheelhouse)]

        pyyaml_source = download_pyyaml_source(downloads, wheelhouse)
        extract_pure_pyyaml(pyyaml_source, output)

        run(common + [
            "install", "--disable-pip-version-check", "--no-compile",
            "--only-binary", ":all:", "--target", str(output),
            "--no-deps", "--platform", "any", "--implementation", "py",
            "--python-version", "3.9", "--abi", "none",
        ] + binary_options + requirements)

    for cache in output.rglob("__pycache__"):
        shutil.rmtree(cache)
    for generated in list(output.rglob("*.pyc")) + list(output.rglob("*.pyo")):
        generated.unlink()

    manifest = ["PyYAML==%s" % PYYAML_VERSION] + requirements
    (output / "VENDOR-MANIFEST.txt").write_text(
        "\n".join(manifest) + "\n", encoding="utf-8"
    )
    errors = validate_vendor(output)
    if errors:
        raise RuntimeError("invalid vendor bundle:\n- " + "\n- ".join(errors))


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Build the pure-Python offline dependency bundle for Venus OS"
    )
    parser.add_argument("--output", type=Path, default=project / "build" / "vendor")
    parser.add_argument(
        "--lock", type=Path, default=project / "requirements-venus.lock"
    )
    parser.add_argument(
        "--wheelhouse", type=Path,
        help="optional directory containing all locked wheels and PyYAML source",
    )
    args = parser.parse_args()
    try:
        build_vendor(args.output, args.lock, args.wheelhouse)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print("Vendor bundle: %s" % args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
