import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


class InstallScriptTests(unittest.TestCase):
    def _fake_commands(self, root: Path) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "id").write_text("#!/bin/sh\necho 0\n", encoding="utf-8")
        (bin_dir / "python3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bin_dir / "svc").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for command in ("id", "python3", "svc"):
            (bin_dir / command).chmod(0o755)
        return bin_dir

    def _run_installer(self, root: Path, **overrides: str) -> subprocess.CompletedProcess:
        target = root / "new"
        service = root / "service" / "new"
        command = root / "data" / "vlm"
        log_dir = root / "logs" / "new"
        legacy = root / "legacy"
        legacy_service = root / "service" / "legacy"
        fake_bin = self._fake_commands(root)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "VLM_TARGET_DIR": str(target),
                "VLM_SERVICE_LINK": str(service),
                "VLM_COMMAND_LINK": str(command),
                "VLM_LOG_DIR": str(log_dir),
                "VLM_LEGACY_TARGET_DIR": str(legacy),
                "VLM_LEGACY_SERVICE_LINK": str(legacy_service),
                "VLM_VENDOR_SOURCE": str(root / "missing-vendor"),
                "VLM_UPDATE_RC_LOCAL": "0",
            }
        )
        env.update(overrides)
        try:
            return subprocess.run(
                ["sh", str(PROJECT_DIR / "install.sh")],
                cwd=PROJECT_DIR,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            self.fail(
                f"installer failed with {exc.returncode}\n"
                f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
            )

    def test_explicit_legacy_migration_preserves_old_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "config.yaml").write_text(
                f"state_file: {legacy}/runtime.json\n"
                "lock_file: /var/run/victron-load-manager.lock\n",
                encoding="utf-8",
            )
            (legacy / "runtime.json").write_text('{"kept": true}\n', encoding="utf-8")
            legacy_service = root / "service" / "legacy"
            legacy_service.parent.mkdir()
            legacy_service.symlink_to(legacy / "service")

            result = self._run_installer(root, VLM_MIGRATE_LEGACY="1")

            target = root / "new"
            config = (target / "config.yaml").read_text(encoding="utf-8")
            self.assertIn(f"state_file: {target}/runtime.json", config)
            self.assertIn("/var/run/versatile-load-manager.lock", config)
            self.assertEqual((target / "runtime.json").read_text(encoding="utf-8"), '{"kept": true}\n')
            self.assertTrue(legacy.is_dir())
            self.assertFalse(legacy_service.exists())
            self.assertTrue((target / "service" / "down").is_file())
            self.assertEqual((target / "log-dir").read_text(encoding="utf-8").strip(), str(root / "logs" / "new"))
            self.assertIn("Migrated legacy configuration", result.stdout)

    def test_custom_install_does_not_migrate_legacy_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "config.yaml").write_text("legacy_marker: true\n", encoding="utf-8")

            self._run_installer(root)

            config = (root / "new" / "config.yaml").read_text(encoding="utf-8")
            self.assertNotIn("legacy_marker", config)
            self.assertIn("dry_run: true", config)


if __name__ == "__main__":
    unittest.main()
