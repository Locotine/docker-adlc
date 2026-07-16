from __future__ import annotations

import importlib.util
import subprocess
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-project.py"


def load_installer_module():
    spec = importlib.util.spec_from_file_location("docker_claude_installer", INSTALLER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load installer module from {INSTALLER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INSTALLER_MODULE = load_installer_module()


class InstallProjectTests(unittest.TestCase):
    def run_installer(self, target: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(INSTALLER), "--target", str(target), *extra],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_merges_into_existing_folders_and_preserves_unrelated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            custom_skill = target / ".claude" / "skills" / "custom" / "SKILL.md"
            custom_script = target / "scripts" / "custom.sh"
            custom_skill.parent.mkdir(parents=True)
            custom_script.parent.mkdir(parents=True)
            custom_skill.write_text("custom skill\n")
            custom_script.write_text("custom script\n")

            result = self.run_installer(target)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(custom_skill.read_text(), "custom skill\n")
            self.assertEqual(custom_script.read_text(), "custom script\n")
            self.assertTrue(
                (target / ".claude" / "skills" / "docker-bootstrap" / "SKILL.md").is_file()
            )
            self.assertFalse((target / ".claude" / "skills" / "bootstrap").exists())
            self.assertTrue((target / ".claude" / "skills" / "infra-up" / "SKILL.md").is_file())
            installed_script = target / "scripts" / "infra-up.sh"
            self.assertTrue(installed_script.is_file())
            self.assertTrue(installed_script.stat().st_mode & stat.S_IXUSR)
            self.assertEqual(
                (target / "scripts" / "docker_contract.py").read_bytes(),
                (ROOT / "scripts" / "docker_contract.py").read_bytes(),
            )
            self.assertFalse((target / "scripts" / "install-project.py").exists())
            self.assertFalse((target / ".claude" / "settings.local.json").exists())

    def test_plugin_managed_file_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            managed_file = target / "scripts" / "infra-up.sh"
            managed_file.parent.mkdir(parents=True)
            managed_file.write_text("outdated project copy\n")

            result = self.run_installer(target)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(managed_file.read_bytes(), (ROOT / "scripts" / "infra-up.sh").read_bytes())
            self.assertTrue(managed_file.stat().st_mode & stat.S_IXUSR)
            self.assertIn("overwritten", result.stdout)
            self.assertIn("1 overwritten", result.stdout)

    def test_legacy_bootstrap_skill_is_preserved_while_new_name_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            legacy_skill = target / ".claude" / "skills" / "bootstrap" / "SKILL.md"
            legacy_skill.parent.mkdir(parents=True)
            legacy_skill.write_text("legacy project-owned skill\n")

            result = self.run_installer(target)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(legacy_skill.read_text(), "legacy project-owned skill\n")
            self.assertTrue(
                (target / ".claude" / "skills" / "docker-bootstrap" / "SKILL.md").is_file()
            )

    def test_second_run_reports_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            first = self.run_installer(target)
            second = self.run_installer(target)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("0 copied", second.stdout)
            self.assertIn("0 overwritten", second.stdout)
            self.assertNotIn("  copied", second.stdout)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)

            result = self.run_installer(target, "--dry-run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would be copied", result.stdout)
            self.assertFalse((target / ".claude").exists())
            self.assertFalse((target / "scripts").exists())

    def test_dry_run_does_not_overwrite_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            managed_file = target / "scripts" / "infra-up.sh"
            managed_file.parent.mkdir(parents=True)
            managed_file.write_text("keep during preview\n")

            result = self.run_installer(target, "--dry-run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(managed_file.read_text(), "keep during preview\n")
            self.assertIn("would overwrite", result.stdout)

    def test_file_blocking_destination_directory_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            blocker = target / "scripts"
            blocker.write_text("keep me\n")

            result = self.run_installer(target)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(blocker.read_text(), "keep me\n")
            self.assertFalse((target / ".claude").exists())
            self.assertIn("error:", result.stderr)

    def test_directory_at_managed_file_path_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            managed_path = target / "scripts" / "infra-up.sh"
            managed_path.mkdir(parents=True)

            result = self.run_installer(target)

            self.assertEqual(result.returncode, 2)
            self.assertTrue(managed_path.is_dir())
            self.assertFalse((target / ".claude").exists())
            self.assertIn("destination is not a regular file", result.stderr)

    def test_symlinked_payload_directory_is_rejected_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external_temp:
            target = Path(temp)
            external = Path(external_temp)
            (target / "scripts").symlink_to(external, target_is_directory=True)

            result = self.run_installer(target)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(external.iterdir()), [])
            self.assertFalse((target / ".claude").exists())
            self.assertRegex(result.stderr, r"destination (escapes|parent must not be a symlink)")

    def test_symlinked_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            real_target = parent / "real-project"
            linked_target = parent / "linked-project"
            real_target.mkdir()
            linked_target.symlink_to(real_target, target_is_directory=True)

            result = self.run_installer(linked_target)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(real_target.iterdir()), [])
            self.assertIn("target directory must not be a symlink", result.stderr)

    def test_symlink_swap_after_preflight_cannot_escape_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external_temp:
            target = Path(temp).resolve()
            external = Path(external_temp)
            plan = INSTALLER_MODULE.build_plan(target)
            (target / ".claude").symlink_to(external, target_is_directory=True)

            with self.assertRaises(OSError):
                INSTALLER_MODULE.install_files(plan, target)

            self.assertEqual(list(external.iterdir()), [])
            self.assertFalse((external / "skills").exists())

    def test_symlinked_managed_file_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external_temp:
            target = Path(temp)
            external = Path(external_temp) / "infra-up.sh"
            external.write_text("external content\n")
            managed_file = target / "scripts" / "infra-up.sh"
            managed_file.parent.mkdir(parents=True)
            managed_file.symlink_to(external)

            result = self.run_installer(target)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(external.read_text(), "external content\n")
            self.assertTrue(managed_file.is_symlink())
            self.assertRegex(
                result.stderr,
                r"destination (escapes target directory|file must not be a symlink)",
            )

    def test_atomic_overwrite_failure_preserves_original_and_cleans_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp).resolve()
            managed_file = target / "scripts" / "infra-up.sh"
            managed_file.parent.mkdir(parents=True)
            managed_file.write_text("original content\n")
            source = ROOT / "scripts" / "infra-up.sh"
            plan = INSTALLER_MODULE.MergePlan(overwrite=[(source, managed_file)])

            def failing_rename(*args, **kwargs):
                raise OSError("simulated rename failure")

            with mock.patch.object(INSTALLER_MODULE.os, "rename", failing_rename):
                INSTALLER_MODULE.os.supports_dir_fd.add(failing_rename)
                try:
                    with self.assertRaises(OSError):
                        INSTALLER_MODULE.install_files(plan, target)
                finally:
                    INSTALLER_MODULE.os.supports_dir_fd.discard(failing_rename)

            self.assertEqual(managed_file.read_text(), "original content\n")
            self.assertEqual(
                list(managed_file.parent.glob(".infra-up.sh.docker-claude.*.tmp")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
