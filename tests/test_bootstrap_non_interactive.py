from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent


def load_infra_init_module():
    path = ROOT / "scripts" / "infra-init.py"
    spec = importlib.util.spec_from_file_location("docker_claude_infra_init_bootstrap", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load infra init module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INFRA_INIT_MODULE = load_infra_init_module()


class BootstrapNonInteractiveTests(unittest.TestCase):
    def test_compose_name_normalization_preserves_valid_separators(self) -> None:
        self.assertEqual(INFRA_INIT_MODULE._normalize_compose_name("foo__bar"), "foo__bar")
        self.assertEqual(INFRA_INIT_MODULE._normalize_compose_name("foo--bar"), "foo--bar")

    def test_port_probe_detects_an_active_host_listener(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)

        self.assertFalse(INFRA_INIT_MODULE._port_is_available(listener.getsockname()[1]))

    def test_detection_allocates_available_ports_across_apps_and_infra(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_node_service(
                project / "api",
                dependencies={"pg": "latest", "redis": "latest"},
                port=4100,
            )

            busy = {4100, 5432, 6379}
            with mock.patch.object(
                INFRA_INIT_MODULE,
                "_port_is_available",
                side_effect=lambda port: port not in busy,
            ):
                report = INFRA_INIT_MODULE.detection_report(project)

            config = report["suggested_config"]
            self.assertEqual(config["services"]["api"]["host_port"], 4101)
            self.assertEqual(config["infra_ports"]["postgres"]["postgres"], 5433)
            self.assertEqual(config["infra_ports"]["redis"]["redis"], 6380)
            unavailable = [
                item for item in report["uncertainties"]
                if item["type"] == "host_port_unavailable"
            ]
            self.assertEqual(
                {(item["scope"], item["name"], item["requested_port"]) for item in unavailable},
                {
                    ("service", "api", 4100),
                    ("infra", "postgres.postgres", 5432),
                    ("infra", "redis.redis", 6379),
                },
            )
            with contextlib.redirect_stdout(io.StringIO()):
                plan = INFRA_INIT_MODULE.build_plan(project, False, config=config)
            infra_yaml = INFRA_INIT_MODULE.render_infra_yaml(plan)
            apps_yaml = INFRA_INIT_MODULE.render_apps_yaml(plan)
            self.assertIn("'5433:5432'", infra_yaml)
            self.assertIn("'6380:6379'", infra_yaml)
            self.assertIn("'4101:4100'", apps_yaml)

    def test_generation_normalizes_images_and_requires_dockerfile_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "DriverPlus @ Boundaries"
            project.mkdir()
            existing = project / "D-IDENTITY-TRUST"
            missing = project / "D-TAXONOMY"
            self._write_node_service(existing, dependencies={"pg": "latest"}, port=4200)
            self._write_node_service(missing, dependencies={"redis": "latest"}, port=4201)
            (existing / "Dockerfile").write_text(
                "FROM node:20-alpine\nRUN groupadd -r app --gid=1000\n"
            )

            report = INFRA_INIT_MODULE.detection_report(project)
            config = report["suggested_config"]
            self.assertEqual(config["project_name"], "driverplus-boundaries")
            self.assertEqual(
                config["services"]["D-IDENTITY-TRUST"]["dockerfile"],
                "service",
            )
            self.assertEqual(config["services"]["D-TAXONOMY"]["dockerfile"], "generated")
            dockerfile_uncertainties = {
                (item["type"], item.get("service")) for item in report["uncertainties"]
            }
            self.assertIn(
                ("dockerfile_fixed_identity", "D-IDENTITY-TRUST"),
                dockerfile_uncertainties,
            )

            # Simulate the reviewed choice to avoid the risky service Dockerfile.
            config["services"]["D-IDENTITY-TRUST"]["dockerfile"] = "generated"
            plan_path = project / "reviewed-plan.json"
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

            self.assertEqual(result.returncode, 0, result.stdout)
            apps_yaml = (project / "infra" / "docker-compose.apps.yml").read_text()
            image_lines = [line.strip() for line in apps_yaml.splitlines() if "image:" in line]
            self.assertEqual(
                image_lines,
                [
                    "image: driverplus-boundaries/d-identity-trust:local",
                    "image: driverplus-boundaries/d-taxonomy:local",
                ],
            )
            self.assertNotIn("container_name:", apps_yaml)
            self.assertIn(
                "dockerfile: ../infra/dockerfiles/Dockerfile.D-IDENTITY-TRUST",
                apps_yaml,
            )
            for service_name in ("D-IDENTITY-TRUST", "D-TAXONOMY"):
                generated = (
                    project / "infra" / "dockerfiles" / f"Dockerfile.{service_name}"
                ).read_text()
                self.assertIn("USER node", generated)
                self.assertNotIn("groupadd", generated)

    def test_dockerfile_identity_detection_handles_multiline_and_arg_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = Path(tmp) / "Dockerfile"
            risky_variants = (
                "FROM node:20-alpine\nRUN groupadd -r app \\\n    --gid 1234\n",
                "FROM node:20-alpine\nARG APP_GID=2345\nRUN addgroup -g $APP_GID app\n",
                "FROM node:20-alpine\nARG UID=3456\nRUN useradd -u ${UID} app\n",
                "FROM node:20-alpine\nARG USER_ID=4567\nRUN adduser -u $USER_ID app\n",
            )
            for content in risky_variants:
                with self.subTest(content=content):
                    dockerfile.write_text(content)
                    self.assertTrue(
                        INFRA_INIT_MODULE._dockerfile_has_fixed_identity(dockerfile)
                    )

            dockerfile.write_text("FROM node:20-alpine\nUSER node\n")
            self.assertFalse(INFRA_INIT_MODULE._dockerfile_has_fixed_identity(dockerfile))

    def test_invalid_service_names_and_normalized_image_collisions_are_blocked(self) -> None:
        cases = (
            ("invalid name", ("bad service",), "invalid_service_name"),
            ("image collision", ("API", "api"), "image_name_collision"),
        )
        for label, service_names, uncertainty_type in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                for index, service_name in enumerate(service_names):
                    self._write_node_service(
                        project / service_name,
                        dependencies={"redis": "latest"},
                        port=4300 + index,
                    )

                report = INFRA_INIT_MODULE.detection_report(project)
                self.assertIn(
                    uncertainty_type,
                    {item["type"] for item in report["uncertainties"]},
                )
                if uncertainty_type == "invalid_service_name":
                    self.assertFalse(
                        report["suggested_config"]["services"][service_names[0]]["include"]
                    )

                result = subprocess.run(
                    [str(ROOT / "scripts" / "infra-init.py"), "--root", str(project), "--yes"],
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

    def test_app_infra_service_name_conflicts_are_reviewed_and_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_node_service(
                project / "redis",
                dependencies={"redis": "latest"},
                port=4400,
            )

            report = INFRA_INIT_MODULE.detection_report(project)
            conflicts = [
                item
                for item in report["uncertainties"]
                if item["type"] == "service_name_conflict"
            ]
            self.assertEqual(conflicts[0]["service"], "redis")
            self.assertEqual(conflicts[0]["infra_modules"], ["redis"])

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
            self.assertIn("conflict with enabled infra services", result.stdout)
            self.assertFalse((project / "infra").exists())

    def test_postgres_secret_key_collisions_are_reviewed_and_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for index, service_name in enumerate(("api-a", "api_a")):
                self._write_node_service(
                    project / service_name,
                    dependencies={"pg": "latest"},
                    port=4500 + index,
                )

            report = INFRA_INIT_MODULE.detection_report(project)
            conflicts = [
                item
                for item in report["uncertainties"]
                if item["type"] == "secret_key_collision"
            ]
            self.assertEqual(conflicts[0]["secret_key"], "API_A_DB_PASSWORD")
            self.assertEqual(conflicts[0]["services"], ["api-a", "api_a"])

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
            self.assertIn("duplicate environment secret keys", result.stdout)
            self.assertFalse((project / "infra").exists())

            # A reviewed plan may resolve the collision by disabling local Postgres.
            config = report["suggested_config"]
            config["infra_modules"] = []
            config["infra_ports"] = {}
            plan_path = project / "reviewed-plan.json"
            plan_path.write_text(json.dumps(config))
            resolved = subprocess.run(
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
            self.assertEqual(resolved.returncode, 0, resolved.stdout)
            apps_yaml = (project / "infra" / "docker-compose.apps.yml").read_text()
            self.assertNotIn("DATABASE_URL", apps_yaml)

    def test_reviewed_config_rejects_a_port_that_became_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(("0.0.0.0", 0))
            listener.listen(1)
            busy_port = listener.getsockname()[1]
            self._write_node_service(project / "api", dependencies={}, port=busy_port)
            config = {
                "project_name": "reviewed",
                "network_name": "reviewed",
                "services": {
                    "api": {
                        "include": True,
                        "host_port": busy_port,
                        "container_port": busy_port,
                        "dockerfile": "generated",
                    }
                },
                "infra_modules": [],
                "infra_ports": {},
            }
            plan_path = project / "plan.json"
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
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("currently in use", result.stdout)
            self.assertFalse((project / "infra").exists())

    def test_yes_bootstraps_fresh_project_without_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "Fresh Project"
            scripts = project / "scripts"
            fake_bin = project / "test-bin"
            scripts.mkdir(parents=True)
            fake_bin.mkdir()

            for name in ("_common.sh", "bootstrap.sh", "infra-init.py", "docker_contract.py"):
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

            for name in ("_common.sh", "bootstrap.sh", "infra-init.py", "docker_contract.py"):
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
                "invalid project name": {**base_config, "project_name": "Reviewed Project"},
                "invalid network name": {**base_config, "network_name": "reviewed network"},
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
                "duplicate app and infra port": {
                    **base_config,
                    "infra_ports": {"postgres": {"postgres": 5000}},
                },
                "unknown infra endpoint": {
                    **base_config,
                    "infra_ports": {"postgres": {"sql": 5432}},
                },
                "infra module ports not object": {
                    **base_config,
                    "infra_ports": {"postgres": 5432},
                },
                "infra endpoint port not integer": {
                    **base_config,
                    "infra_ports": {"postgres": {"postgres": "standard"}},
                },
                "invalid dockerfile strategy": {
                    **base_config,
                    "services": {
                        **base_config["services"],
                        "api-a": {**base_config["services"]["api-a"], "dockerfile": "guess"},
                    },
                },
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
            for name in ("_common.sh", "bootstrap.sh", "infra-init.py", "docker_contract.py"):
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

    def test_bootstrap_fails_when_strict_verify_reports_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            scripts = project / "scripts"
            infra = project / "infra"
            contracts = infra / "contracts"
            fake_bin = project / "test-bin"
            scripts.mkdir()
            contracts.mkdir(parents=True)
            fake_bin.mkdir()
            for name in ("_common.sh", "bootstrap.sh"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            self._write_executable(
                scripts / "infra-up.sh",
                "#!/usr/bin/env bash\nset -euo pipefail\necho provisioned\n",
            )
            self._write_executable(
                scripts / "sync-env-docker.py",
                "#!/usr/bin/env bash\necho '[MISSING] REQUIRED_KEY'\nexit 1\n",
            )
            self._write_executable(fake_bin / "docker", "#!/usr/bin/env bash\nexit 0\n")
            (infra / "docker-compose.infra.yml").write_text("name: strict-test\nservices: {}\n")
            (infra / "docker-compose.apps.yml").write_text("services:\n  api:\n    image: api\n")
            (infra / ".env").write_text("SECRET=local\n")
            (contracts / "env.json").write_text(json.dumps({"services": {"api": {}}}))
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

            result = subprocess.run(
                [str(scripts / "bootstrap.sh"), "--yes"],
                cwd=project,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("bootstrap failed", result.stdout)
            self.assertNotIn("done.", result.stdout)

    def test_bootstrap_never_invents_an_external_app_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            scripts = project / "scripts"
            infra = project / "infra"
            scripts.mkdir()
            infra.mkdir()
            for name in ("_common.sh", "bootstrap.sh"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            self._write_executable(
                scripts / "infra-up.sh",
                "#!/usr/bin/env bash\necho should-not-run\nexit 0\n",
            )
            (infra / "docker-compose.infra.yml").write_text(
                "name: external-secret\nservices: {}\n"
            )
            (infra / ".env.example").write_text(
                "STRIPE_API_KEY=REPLACE_ME_STRIPE_API_KEY\n"
            )

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
            self.assertIn("STRIPE_API_KEY", result.stdout)
            self.assertIn("never invents credentials", result.stdout)
            self.assertNotIn("should-not-run", result.stdout)
            self.assertIn("REPLACE_ME_STRIPE_API_KEY", (infra / ".env").read_text())
            self.assertEqual(stat.S_IMODE((infra / ".env").stat().st_mode), 0o600)

    def test_infra_up_derives_urlencoded_database_password_without_changing_raw_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            scripts = project / "scripts"
            infra = project / "infra"
            contracts = infra / "contracts"
            fake_bin = project / "test-bin"
            scripts.mkdir()
            contracts.mkdir(parents=True)
            fake_bin.mkdir()
            for name in ("_common.sh", "infra-up.sh"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            (infra / "docker-compose.infra.yml").write_text(
                "name: encoded-secret\nservices: {}\n"
            )
            (infra / ".env").write_text("APP_DB_PASSWORD='slash/#at@value'\n")
            (contracts / "postgres.json").write_text(json.dumps({
                "databases": [{"name": "app", "password_env": "APP_DB_PASSWORD"}],
            }))
            self._write_executable(
                fake_bin / "docker",
                "#!/usr/bin/env bash\nexit 0\n",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

            result = subprocess.run(
                [str(scripts / "infra-up.sh"), "--infra-only"],
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
            env_text = (infra / ".env").read_text()
            self.assertIn("APP_DB_PASSWORD='slash/#at@value'", env_text)
            self.assertIn(
                "APP_DB_PASSWORD_URLENCODED=slash%2F%23at%40value", env_text
            )
            self.assertEqual(stat.S_IMODE((infra / ".env").stat().st_mode), 0o600)

    def test_infra_up_orders_reconcile_migrate_and_app_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            scripts = project / "scripts"
            infra = project / "infra"
            contracts = infra / "contracts"
            fake_bin = project / "test-bin"
            scripts.mkdir()
            contracts.mkdir(parents=True)
            fake_bin.mkdir()
            for name in ("_common.sh", "infra-up.sh"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            (infra / "docker-compose.infra.yml").write_text("name: ordered\nservices: {}\n")
            (infra / "docker-compose.apps.yml").write_text("services: {}\n")
            (infra / ".env").write_text("SECRET=local\n")
            (contracts / "env.json").write_text(json.dumps({"services": {"api": {}}}))
            log = project / "docker.log"
            self._write_executable(
                fake_bin / "docker",
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >>\"$DOCKER_LOG\"\n"
                "case \"$*\" in\n"
                "  *'config --services'*) printf '%s\\n' postgres postgres-provision keycloak-provision kafka-provision api api-migrate ;;\n"
                "esac\n"
                "exit 0\n",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["DOCKER_LOG"] = str(log)

            result = subprocess.run(
                [str(scripts / "infra-up.sh"), "--build"],
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
            commands = log.read_text().splitlines()
            indices = {
                "postgres_up": next(i for i, line in enumerate(commands) if "up -d --wait postgres" in line),
                "postgres_reconcile": next(i for i, line in enumerate(commands) if "run --rm postgres-provision" in line),
                "keycloak": next(i for i, line in enumerate(commands) if "run --rm keycloak-provision" in line),
                "kafka": next(i for i, line in enumerate(commands) if "run --rm kafka-provision" in line),
                "build": next(i for i, line in enumerate(commands) if " build api" in line),
                "migrate": next(i for i, line in enumerate(commands) if "run --rm api-migrate" in line),
                "apps": next(i for i, line in enumerate(commands) if "up -d --wait --build api" in line),
            }
            self.assertEqual(list(indices.values()), sorted(indices.values()))

    def test_infra_up_stops_before_build_when_provisioning_fails(self) -> None:
        result, commands = self._run_infra_up_with_docker_failure(
            "run --rm keycloak-provision"
        )

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(any("run --rm keycloak-provision" in line for line in commands))
        self.assertFalse(any(" build api" in line for line in commands))
        self.assertFalse(any("up -d --wait api" in line for line in commands))

    def test_infra_up_stops_before_apps_and_explains_failed_migration(self) -> None:
        result, commands = self._run_infra_up_with_docker_failure("run --rm api-migrate")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("prisma migrate resolve", result.stdout)
        self.assertTrue(any("run --rm api-migrate" in line for line in commands))
        self.assertFalse(any("up -d --wait api" in line for line in commands))

    def _run_infra_up_with_docker_failure(
        self, failure_fragment: str
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name)
        scripts = project / "scripts"
        infra = project / "infra"
        contracts = infra / "contracts"
        fake_bin = project / "test-bin"
        scripts.mkdir()
        contracts.mkdir(parents=True)
        fake_bin.mkdir()
        for name in ("_common.sh", "infra-up.sh"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (infra / "docker-compose.infra.yml").write_text("name: failure-test\nservices: {}\n")
        (infra / "docker-compose.apps.yml").write_text("services: {}\n")
        (infra / ".env").write_text("SECRET=local\n")
        (contracts / "env.json").write_text(json.dumps({"services": {"api": {}}}))
        log = project / "docker.log"
        self._write_executable(
            fake_bin / "docker",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >>\"$DOCKER_LOG\"\n"
            "case \"$*\" in\n"
            "  *'config --services'*) printf '%s\\n' postgres postgres-provision keycloak-provision kafka-provision api api-migrate ;;\n"
            "esac\n"
            "case \"$*\" in\n"
            "  *\"$FAIL_AT\"*) exit 17 ;;\n"
            "esac\n"
            "exit 0\n",
        )
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env["DOCKER_LOG"] = str(log)
        env["FAIL_AT"] = failure_fragment
        result = subprocess.run(
            [str(scripts / "infra-up.sh")],
            cwd=project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
            check=False,
        )
        return result, log.read_text().splitlines()

    @staticmethod
    def _write_node_service(path: Path, dependencies: Dict[str, str], port: int) -> None:
        path.mkdir()
        (path / "package.json").write_text(json.dumps({
            "engines": {"node": ">=24.0.0"},
            "scripts": {"build": "echo build", "prod": "node dist/main.js"},
            "dependencies": dependencies,
        }))
        (path / ".env.example").write_text(f"PORT={port}\n")

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content)
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
