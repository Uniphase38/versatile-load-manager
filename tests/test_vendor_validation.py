import tempfile
import subprocess
import tarfile
import unittest
from pathlib import Path

from tools.validate_vendor import REQUIRED_PACKAGES, validate_vendor
from tools.build_vendor import extract_pure_pyyaml, pinned_requirements


class VendorValidationTests(unittest.TestCase):
    def _vendor(self, root: Path) -> Path:
        vendor = root / "vendor"
        for package in REQUIRED_PACKAGES:
            directory = vendor / package
            directory.mkdir(parents=True)
            (directory / "__init__.py").write_text("", encoding="utf-8")
        return vendor

    def test_complete_pure_python_vendor_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            vendor = self._vendor(Path(directory))
            self.assertEqual(validate_vendor(vendor), [])

    def test_missing_package_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            vendor = self._vendor(Path(directory))
            (vendor / "yaml" / "__init__.py").unlink()
            self.assertIn("missing Python package: yaml", validate_vendor(vendor))

    def test_compiled_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            vendor = self._vendor(Path(directory))
            (vendor / "yaml" / "_yaml.so").write_bytes(b"not portable")
            errors = validate_vendor(vendor)
            self.assertTrue(any("compiled extension" in error for error in errors))

    def test_release_builder_creates_archive_and_checksum(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = self._vendor(root)
            output = root / "dist"
            subprocess.run(
                [
                    str(project / "tools" / "build-venus-release.sh"),
                    "--vendor", str(vendor),
                    "--output", str(output),
                ],
                cwd=project,
                check=True,
                capture_output=True,
                text=True,
            )
            archive = output / "victron-load-manager-0.1.0-venus.tar.gz"
            self.assertTrue(archive.is_file())
            self.assertTrue(Path(str(archive) + ".sha256").is_file())
            with tarfile.open(archive, "r:gz") as package:
                names = package.getnames()
            prefix = "victron-load-manager-0.1.0-venus"
            self.assertIn(prefix + "/install.sh", names)
            self.assertIn(prefix + "/vendor/yaml/__init__.py", names)

    def test_venus_requirements_are_exactly_pinned(self):
        project = Path(__file__).resolve().parents[1]
        values = pinned_requirements(project / "requirements-venus.lock")
        self.assertIn("pymodbus==2.5.3", values)
        self.assertTrue(all("==" in value for value in values))

    def test_unpinned_requirement_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "requirements.lock"
            lock.write_text("requests>=2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not exactly pinned"):
                pinned_requirements(lock)

    def test_pure_pyyaml_package_is_extracted_from_source_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source" / "PyYAML-6.0.2" / "lib" / "yaml"
            source_root.mkdir(parents=True)
            (source_root / "__init__.py").write_text("VERSION = 'test'\n")
            archive = root / "PyYAML-6.0.2.tar.gz"
            with tarfile.open(archive, "w:gz") as package:
                package.add(root / "source" / "PyYAML-6.0.2", arcname="PyYAML-6.0.2")
            vendor = root / "vendor"
            vendor.mkdir()
            extract_pure_pyyaml(archive, vendor)
            self.assertEqual(
                (vendor / "yaml" / "__init__.py").read_text(),
                "VERSION = 'test'\n",
            )
