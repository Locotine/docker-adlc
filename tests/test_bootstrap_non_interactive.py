from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict


ROOT = Path(__file__).resolve().parent.parent


class BootstrapNonInteractiveTests(unittest.TestCase):
    def test_yes_bootstraps_fresh_project_without_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "Fresh Project"
            scripts = project / "scripts"
            fake_bin = project / "test-bin"
            scripts.mkdir(parents=True)
            fake_bin.mkdir()

            for name in ("_common.sh", "bootstrap.sh", "infra-init.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)

            self._write_executable(
                scripts / "infra-up.sh",
                "#!/usr/bin/env bash\nset -euo pipefail\necho 'fake infra-up complete'\n",
            )
            self._write_executable(
                fake_bin / "docker",
                "#!/usr/bin/env bash\nexit 0\n",
            )

            self._write_node_service(
                project / "auth-api",
                dependencies={"jose": "latest"},
                port=4100,
            )
            self._write_node_service(
                project / "workflow-api",
                dependencies={"@temporalio/client": "latest"},
                port=4100,
            )

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            result = subprocess.run(
                [str(scripts / "bootstrap.sh"), "--yes", "--skip-verify"],
                cwd=project,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("--yes: using detected defaults without prompting", result.stdout)
            self.assertIn("fake infra-up complete", result.stdout)
            self.assertIn("done.", result.stdout)

            infra = project / "infra"
            self.assertTrue((infra / ".env").is_file())
            env_text = (infra / ".env").read_text()
            secret_values = [
                line.split("=", 1)[1]
                for line in env_text.splitlines()
                if line and not line.startswith("#") and "=" in line
            ]
            self.assertNotIn("REPLACE_ME_", env_text)
            self.assertTrue(secret_values)
            self.assertTrue(all(len(value) >= 32 for value in secret_values))
            self.assertTrue(all(value not in result.stdout for value in secret_values))
            self.assertEqual(stat.S_IMODE((infra / ".env").stat().st_mode), 0o600)
            infra_yaml = (infra / "docker-compose.infra.yml").read_text()
            apps_yaml = (infra / "docker-compose.apps.yml").read_text()

            # Keycloak and Temporal both need the generated Postgres service.
            self.assertIn("  postgres:", infra_yaml)
            self.assertIn("  keycloak:", infra_yaml)
            self.assertIn("  temporal:", infra_yaml)

            # Duplicate detected ports are made unique automatically.
            self.assertIn("'4100:4100'", apps_yaml)
            self.assertIn("'4101:4100'", apps_yaml)

    def test_detect_then_reviewed_config_runs_without_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "Reviewed Project"
            scripts = project / "scripts"
            fake_bin = project / "test-bin"
            scripts.mkdir(parents=True)
            fake_bin.mkdir()

            for name in ("_common.sh", "bootstrap.sh", "infra-init.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            self._write_executable(
                scripts / "infra-up.sh",
                "#!/usr/bin/env bash\nset -euo pipefail\necho 'fake infra-up complete'\n",
            )
            self._write_executable(fake_bin / "docker", "#!/usr/bin/env bash\nexit 0\n")
            self._write_node_service(
                project / "api-a",
                dependencies={"@nestjs/microservices": "latest"},
                port=5000,
            )
            self._write_node_service(
                project / "api-b",
                dependencies={"redis": "latest"},
                port=5000,
            )

            detect = subprocess.run(
                [str(scripts / "infra-init.py"), "--root", str(project), "--detect-json"],
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(detect.returncode, 0, detect.stderr)
            report = json.loads(detect.stdout)
            uncertainty_types = {item["type"] for item in report["uncertainties"]}
            self.assertIn("ambiguous_module", uncertainty_types)
            self.assertIn("duplicate_port", uncertainty_types)
            self.assertFalse((project / "infra").exists())

            # Simulate the agent applying the user's Grill Me answers.
            report["suggested_config"]["services"]["api-b"]["host_port"] = 5100
            report["suggested_config"]["infra_modules"] = ["redis"]
            plan_path = project / "reviewed-plan.json"
            plan_path.write_text(json.dumps(report))

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            result = subprocess.run(
                [
                    str(scripts / "bootstrap.sh"),
                    "--init-config",
                    str(plan_path),
                    "--skip-verify",
                ],
                cwd=project,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("--config: using reviewed choices without prompting", result.stdout)
            apps_yaml = (project / "infra" / "docker-compose.apps.yml").read_text()
            infra_yaml = (project / "infra" / "docker-compose.infra.yml").read_text()
            self.assertIn("'5100:5000'", apps_yaml)
            self.assertIn("  redis:", infra_yaml)
            self.assertNotIn("  redpanda:", infra_yaml)

    def test_detect_no_candidates_reports_uncertainty_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            result = subprocess.run(
                [str(ROOT / "scripts" / "infra-init.py"), "--root", str(project), "--detect-json"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["candidates"], [])
            self.assertEqual(report["uncertainties"][0]["type"], "no_candidates")
            self.assertFalse((project / "infra").exists())

    def test_non_node_service_without_dockerfile_fails_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            service = project / "worker"
            service.mkdir()
            (service / "requirements.txt").write_text("temporalio==1.7.0\n")

            detect = subprocess.run(
                [str(ROOT / "scripts" / "infra-init.py"), "--root", str(project), "--detect-json"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(detect.returncode, 0, detect.stderr)
            uncertainty_types = {
                item["type"] for item in json.loads(detect.stdout)["uncertainties"]
            }
            self.assertIn("missing_port", uncertainty_types)
            self.assertIn("missing_dockerfile", uncertainty_types)

            result = subprocess.run(
                [str(ROOT / "scripts" / "infra-init.py"), "--root", str(project), "--yes"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("has no Dockerfile", result.stdout)
            self.assertFalse((project / "infra").exists())

    def test_detect_node_service_without_infra_reports_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_node_service(project / "plain-api", dependencies={}, port=3000)

            result = subprocess.run(
                [str(ROOT / "scripts" / "infra-init.py"), "--root", str(project), "--detect-json"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["suggested_config"]["infra_modules"], [])
            self.assertIn(
                "no_infra_detected",
                {item["type"] for item in report["uncertainties"]},
            )

    def test_reviewed_config_validation_errors_never_write_infra(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_node_service(
                project / "api-a",
                dependencies={"redis": "latest"},
                port=5000,
            )
            self._write_node_service(
                project / "api-b",
                dependencies={"@temporalio/client": "latest"},
                port=5001,
            )
            base_config = {
                "project_name": "reviewed",
                "network_name": "reviewed",
                "services": {
                    "api-a": {"include": True, "host_port": 5000, "container_port": 5000},
                    "api-b": {"include": True, "host_port": 5001, "container_port": 5001},
                },
                "infra_modules": ["postgres", "redis", "temporal"],
            }
            invalid_cases = {
                "unknown service": {
                    **base_config,
                    "services": {**base_config["services"], "ghost": {"include": True}},
                },
                "boolean include": {
                    **base_config,
                    "services": {
                        **base_config["services"],
                        "api-a": {**base_config["services"]["api-a"], "include": "yes"},
                    },
                },
                "out of range port": {
                    **base_config,
                    "services": {
                        **base_config["services"],
                        "api-a": {**base_config["services"]["api-a"], "host_port": 70000},
                    },
                },
                "duplicate host port": {
                    **base_config,
                    "services": {
                        **base_config["services"],
                        "api-b": {**base_config["services"]["api-b"], "host_port": 5000},
                    },
                },
                "unknown module": {**base_config, "infra_modules": ["redis", "oracle"]},
                "missing postgres": {**base_config, "infra_modules": ["temporal"]},
            }

            for label, config in invalid_cases.items():
                with self.subTest(label=label):
                    plan_path = project / "invalid-plan.json"
                    plan_path.write_text(json.dumps(config))
                    result = subprocess.run(
                        [
                            str(ROOT / "scripts" / "infra-init.py"),
                            "--root",
                            str(project),
                            "--config",
                            str(plan_path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("error:", result.stdout)
                    self.assertFalse((project / "infra").exists())

    def test_bootstrap_rejects_invalid_cli_without_reading_stdin(self) -> None:
        cases = (["--init-config"], ["--not-a-real-flag"])
        for args in cases:
            with self.subTest(args=args):
                result = subprocess.run(
                    [str(ROOT / "scripts" / "bootstrap.sh"), *args],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stdout)

    def test_bootstrap_propagates_nested_init_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            scripts = project / "scripts"
            scripts.mkdir()
            for name in ("_common.sh", "bootstrap.sh", "infra-init.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)

            result = subprocess.run(
                [str(scripts / "bootstrap.sh"), "--yes", "--skip-verify"],
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("no service candidates found", result.stdout)
            self.assertFalse((project / "infra").exists())

    @staticmethod
    def _write_node_service(path: Path, dependencies: Dict[str, str], port: int) -> None:
        path.mkdir()
        (path / "package.json").write_text(json.dumps({"dependencies": dependencies}))
        (path / ".env.example").write_text(f"PORT={port}\n")

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content)
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
