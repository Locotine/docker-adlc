from __future__ import annotations

import importlib.util
import subprocess
import stat
import sys
import tempfile
import unittest
from pathlib import Path


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
            self.assertTrue((target / ".claude" / "skills" / "infra-up" / "SKILL.md").is_file())
            installed_script = target / "scripts" / "infra-up.sh"
            self.assertTrue(installed_script.is_file())
            self.assertTrue(installed_script.stat().st_mode & stat.S_IXUSR)
            self.assertFalse((target / "scripts" / "install-project.py").exists())
            self.assertFalse((target / ".claude" / "settings.local.json").exists())

    def test_conflicting_file_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            conflict = target / "scripts" / "infra-up.sh"
            conflict.parent.mkdir(parents=True)
            conflict.write_text("project-owned content\n")

            result = self.run_installer(target)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(conflict.read_text(), "project-owned content\n")
            self.assertIn("conflict", result.stdout)
            self.assertIn("1 conflicts kept", result.stdout)

    def test_second_run_reports_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            first = self.run_installer(target)
            second = self.run_installer(target)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("0 copied", second.stdout)
            self.assertIn("0 conflicts kept", second.stdout)
            self.assertNotIn("  copied", second.stdout)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)

            result = self.run_installer(target, "--dry-run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would be copied", result.stdout)
            self.assertFalse((target / ".claude").exists())
            self.assertFalse((target / "scripts").exists())

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
                INSTALLER_MODULE.copy_missing_files(plan, target)

            self.assertEqual(list(external.iterdir()), [])
            self.assertFalse((external / "skills").exists())


if __name__ == "__main__":
    unittest.main()
