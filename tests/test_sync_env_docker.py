from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = ROOT / "scripts" / "sync-env-docker.py"


def load_sync_module():
    spec = importlib.util.spec_from_file_location("docker_claude_sync_env", SYNC_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load sync module from {SYNC_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SYNC_MODULE = load_sync_module()


class SyncEnvSecretRedactionTests(unittest.TestCase):
    def run_verify(self, actual_value: str, expected_pattern: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with (
            mock.patch.object(
                SYNC_MODULE,
                "load_env_example",
                return_value=({"API_SECRET": ""}, {"API_SECRET": expected_pattern}),
            ),
            mock.patch.object(
                SYNC_MODULE,
                "docker_env",
                return_value={"API_SECRET": actual_value},
            ),
            mock.patch.object(
                SYNC_MODULE,
                "resolve_app_containers",
                return_value=["test-service-1"],
            ),
            mock.patch.object(
                SYNC_MODULE,
                "container_health",
                return_value=(True, "running"),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            result = SYNC_MODULE.cmd_verify("test-service", skip_infra=True)
        return result, stdout.getvalue()

    def test_mismatch_does_not_print_actual_secret(self) -> None:
        secret = "live-super-secret-value"

        result, output = self.run_verify(secret, r"^expected-format$")

        self.assertEqual(result, 1)
        self.assertIn("MISMATCH", output)
        self.assertIn("API_SECRET", output)
        self.assertIn(f"length={len(secret)}", output)
        self.assertNotIn(secret, output)

    def test_placeholder_does_not_print_actual_value(self) -> None:
        placeholder = "REPLACE_ME-super-secret-value"

        result, output = self.run_verify(placeholder, r"^REPLACE_ME-.+$")

        self.assertEqual(result, 1)
        self.assertIn("PLACEHOLDER", output)
        self.assertIn(f"length={len(placeholder)}", output)
        self.assertNotIn(placeholder, output)

    def test_extra_and_secret_drift_are_hard_failures_without_value_disclosure(self) -> None:
        actual_secret = "container-only-secret"
        source_secret = "infra-dotenv-secret"
        stdout = io.StringIO()
        contract = {
            "required_keys": ["API_SECRET"],
            "expected": {},
            "platform_allowlist": [],
            "secret_checks": [{
                "secret_env": "API_SECRET",
                "container_key": "API_SECRET",
                "mode": "direct",
            }],
        }
        with (
            mock.patch.object(SYNC_MODULE, "load_env_example", return_value=({"API_SECRET": ""}, {})),
            mock.patch.object(SYNC_MODULE, "load_service_contract", return_value=contract),
            mock.patch.object(SYNC_MODULE, "resolve_app_containers", return_value=["api-1"]),
            mock.patch.object(
                SYNC_MODULE,
                "docker_env",
                return_value={"API_SECRET": actual_secret, "GENERATOR_GUESS": "bad"},
            ),
            mock.patch.object(SYNC_MODULE, "load_dotenv", return_value={"API_SECRET": source_secret}),
            mock.patch.object(SYNC_MODULE, "container_health", return_value=(True, "healthy")),
            contextlib.redirect_stdout(stdout),
        ):
            result = SYNC_MODULE.cmd_verify("api", skip_infra=True)

        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("EXTRA", output)
        self.assertIn("SECRET_DRIFT", output)
        self.assertNotIn(actual_secret, output)
        self.assertNotIn(source_secret, output)

    def test_label_resolution_returns_every_scaled_replica(self) -> None:
        completed = mock.Mock(returncode=0, stdout="project-api-2\nproject-api-1\n", stderr="")
        with (
            mock.patch.object(SYNC_MODULE, "run_capture", return_value=completed) as runner,
            mock.patch.object(SYNC_MODULE, "_compose_project_name", return_value="project"),
        ):
            containers = SYNC_MODULE.resolve_app_containers("api")

        self.assertEqual(containers, ["project-api-1", "project-api-2"])
        command = runner.call_args.args[0]
        self.assertIn("-a", command)
        self.assertIn("label=com.docker.compose.service=api", command)
        self.assertIn("label=com.docker.compose.oneoff=False", command)
        self.assertIn("label=com.docker.compose.project=project", command)

    def test_pg_parser_supports_hyphens_and_psql_variable_password_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pg = root / "infra" / "pg-init"
            pg.mkdir(parents=True)
            (pg / "00.sql").write_text(
                "CREATE USER \"d-identity-trust\" WITH PASSWORD :'role_password';\n"
                "CREATE DATABASE \"d-identity-trust\" OWNER \"d-identity-trust\";\n"
            )
            users, databases = SYNC_MODULE.parse_pg_init(root)

        self.assertIn("d-identity-trust", users)
        self.assertIn("d-identity-trust", databases)

    def test_command_listener_ports_include_redpanda_internal_listener(self) -> None:
        inspect = {
            "Config": {
                "Entrypoint": [],
                "Cmd": [
                    "redpanda", "start",
                    "--kafka-addr=PLAINTEXT://0.0.0.0:29092,OUTSIDE://0.0.0.0:9092",
                ],
            }
        }
        self.assertEqual(SYNC_MODULE._command_ports(inspect), {9092, 29092})

    def test_cross_check_infra_validates_alias_ports_and_postgres_contract(self) -> None:
        infra_map = {
            "postgres": [{"internal_ports": [5432], "host_ports": {}, "container": "pg"}],
            "redpanda": [{"internal_ports": [29092], "host_ports": {}, "container": "rp"}],
        }

        problems = SYNC_MODULE.cross_check_infra(
            "api-1",
            {
                "DATABASE_URL": "postgresql://unknown@postgres:5432/unknown-db",
                "KAFKA_BROKERS": "redpanda:29092",
                "REDIS_HOST": "localhost",
            },
            infra_map,
            {"app": {"databases": {"app"}}},
            {"app"},
        )

        severities = {problem[0] for problem in problems}
        self.assertEqual(severities, {"INFRA_DB", "INFRA_HOST", "INFRA_USER"})
        self.assertFalse(any(key == "KAFKA_BROKERS" for _, key, _ in problems))

    def test_keycloak_contract_checks_roles_clients_and_service_account_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contracts = root / "infra" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "keycloak.json").write_text(json.dumps({
                "realms": [{
                    "name": "local",
                    "roles": ["developer"],
                    "clients": [{
                        "client_id": "local-admin",
                        "kind": "service-account",
                        "secret_env": "LOCAL_ADMIN_SECRET",
                        "roles": ["reader"],
                    }],
                }],
            }))

            def http_json(url: str, **kwargs):
                if url.endswith("/realms/master/protocol/openid-connect/token"):
                    return 200, {"access_token": "admin-token"}
                if "clientId=local-admin" in url:
                    return 200, [{"id": "client-uuid"}]
                if url.endswith("/realms/local/protocol/openid-connect/token"):
                    return 200, {"access_token": "client-token"}
                return 200, {}

            infra_map = {
                "keycloak": [{
                    "host_ports": {8080: 18080},
                    "internal_ports": [8080],
                    "container": "keycloak-1",
                }],
            }
            with (
                mock.patch.object(SYNC_MODULE, "REPO_ROOT", root),
                mock.patch.object(SYNC_MODULE, "_http_json", side_effect=http_json),
                mock.patch.object(SYNC_MODULE, "probe_http", return_value=(True, 200)),
            ):
                problems = SYNC_MODULE.verify_keycloak_contract(
                    infra_map,
                    {
                        "KEYCLOAK_ADMIN_PASSWORD": "admin-secret",
                        "LOCAL_ADMIN_SECRET": "client-secret",
                    },
                )

        self.assertEqual(problems, [])

    def test_kafka_contract_reports_missing_topic_and_non_strict_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contracts = root / "infra" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "kafka.json").write_text(json.dumps({
                "strict": True,
                "topics": [
                    {
                        "name": "present.v1",
                        "partitions": 1,
                        "replication_factor": 1,
                        "config": {"cleanup.policy": "compact"},
                    },
                    {"name": "missing.v1"},
                ],
            }))
            topic_describe = mock.Mock(
                returncode=0,
                stdout=(
                    "SUMMARY\n=======\nNAME        PARTITIONS  REPLICAS\n"
                    "present.v1  2           1\n\nCONFIGS\n=======\n"
                    "KEY             VALUE   SOURCE\n"
                    "cleanup.policy  delete  DYNAMIC_TOPIC_CONFIG\n"
                ),
                stderr="",
            )
            topic_missing = mock.Mock(returncode=1, stdout="", stderr="missing")
            cluster_config = mock.Mock(returncode=0, stdout="true\n", stderr="")
            with (
                mock.patch.object(SYNC_MODULE, "REPO_ROOT", root),
                mock.patch.object(
                    SYNC_MODULE,
                    "run_capture",
                    side_effect=[topic_missing, topic_describe, cluster_config],
                ),
            ):
                problems = SYNC_MODULE.verify_kafka_contract({
                    "redpanda": [{"container": "redpanda-1"}],
                })

        self.assertEqual(
            {problem[0] for problem in problems},
            {"KAFKA_CONFIG", "KAFKA_PARTITIONS", "KAFKA_STRICT", "KAFKA_TOPIC"},
        )

    def test_gen_local_rewrites_split_host_port_and_broker_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = root / "api"
            service.mkdir()
            (service / ".env.example").write_text(
                "REDIS_HOST=redis\nREDIS_PORT=6379\nKAFKA_BROKERS=redpanda:29092\n"
            )
            target = service / ".env.local"
            with (
                mock.patch.object(SYNC_MODULE, "REPO_ROOT", root),
                mock.patch.object(SYNC_MODULE, "discover_host_ports", return_value={
                    "redis": ("localhost", 6380),
                    "redpanda": ("localhost", 9093),
                }),
            ):
                result = SYNC_MODULE.cmd_gen_local("api", str(target))

            self.assertEqual(result, 0)
            self.assertEqual(
                target.read_text(),
                "REDIS_HOST=localhost\nREDIS_PORT=6380\nKAFKA_BROKERS=localhost:9093\n",
            )


if __name__ == "__main__":
    unittest.main()
