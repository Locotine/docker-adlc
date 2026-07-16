from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "driverplus-like"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONTRACT = load_module("docker_contract_fixture_test", ROOT / "scripts" / "docker_contract.py")
INFRA = load_module("infra_init_fixture_test", ROOT / "scripts" / "infra-init.py")


class DockerContractAuditTests(unittest.TestCase):
    def test_audit_unions_env_source_prisma_health_and_production_topics(self) -> None:
        taxonomy = CONTRACT.audit_service(FIXTURE / "d-taxonomy")
        identity = CONTRACT.audit_service(FIXTURE / "d-identity-trust")

        self.assertEqual(taxonomy.node_major, 24)
        self.assertEqual(taxonomy.start_script, "prod")
        self.assertTrue(taxonomy.prisma)
        self.assertEqual(taxonomy.prisma_schemas, ["taxonomy", "taxonomy_audit"])
        self.assertEqual(taxonomy.health_candidates[0], "/health/ready")
        self.assertIn("DATABASE_URL", taxonomy.env)
        self.assertIn("prisma:schema.prisma:env", taxonomy.env["DATABASE_URL"].sources)
        self.assertIn("KEYCLOAK_ADMIN_CLIENT_SECRET", identity.code_env_keys)
        self.assertNotIn("KEYCLOAK_ADMIN_CLIENT_SECRET", identity.env_example_keys)
        self.assertIn("identity.user.profile.registered.v1", identity.topics)
        self.assertIn("notification.dispatch.requested.high.v1", identity.topics)
        self.assertNotIn("ignored.topic.from_comment.v1", identity.topics)
        self.assertNotIn("test.only.topic.v1", identity.topics)

    def test_engine_range_selects_lowest_satisfying_major(self) -> None:
        self.assertEqual(CONTRACT.node_major_from_engine(">=24.0.0"), 24)
        self.assertEqual(CONTRACT.node_major_from_engine(">=22 <25"), 22)
        self.assertEqual(CONTRACT.node_major_from_engine("20 || >=24"), 20)
        self.assertEqual(CONTRACT.node_major_from_engine(None), 24)


class GeneratedLocalStackTests(unittest.TestCase):
    def test_force_regeneration_preserves_old_credentials_and_appends_new_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = root / "previous.env"
            example = root / ".env.example"
            target = root / ".env"
            previous.write_text("POSTGRES_PASSWORD=existing-volume-secret\nCUSTOM=value\n")
            example.write_text(
                "POSTGRES_PASSWORD=GENERATE_ME_POSTGRES_PASSWORD\n"
                "NEW_LOCAL=GENERATE_ME_NEW_LOCAL\n"
                "EXTERNAL_API_KEY=REPLACE_ME_EXTERNAL_API_KEY\n"
            )

            INFRA._merge_preserved_env(previous, example, target)

            merged = target.read_text()
            self.assertIn("POSTGRES_PASSWORD=existing-volume-secret", merged)
            self.assertNotIn("GENERATE_ME_POSTGRES_PASSWORD", merged)
            self.assertIn("CUSTOM=value", merged)
            self.assertIn("NEW_LOCAL=GENERATE_ME_NEW_LOCAL", merged)
            self.assertIn("EXTERNAL_API_KEY=REPLACE_ME_EXTERNAL_API_KEY", merged)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_generator_and_contracts_cover_prisma_env_realms_topics_and_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "Boundaries"
            shutil.copytree(FIXTURE, project)
            with mock.patch.object(INFRA, "_port_is_available", return_value=True):
                report = INFRA.detection_report(project)

            uncertainty_types = {item["type"] for item in report["uncertainties"]}
            self.assertIn("env_contract_drift", uncertainty_types)
            self.assertIn("keycloak_client_realm_ambiguous", uncertainty_types)
            config = report["suggested_config"]
            config["infra_modules"] = ["postgres", "redis", "kafka", "keycloak", "temporal"]
            config["infra_ports"] = {
                key: value for key, value in config["infra_ports"].items()
                if key in config["infra_modules"]
            }
            identity = config["services"]["d-identity-trust"]
            identity["env"].update({
                "KEYCLOAK_URL": "http://localhost:8080",
                "KEYCLOAK_REALM_P1": "dp-p1",
                "KEYCLOAK_REALM_P2": "dp-p2",
                "KEYCLOAK_ADMIN_CLIENT_ID": "identity-admin",
                "KEYCLOAK_ADMIN_CLIENT_SECRET": None,
            })
            config["keycloak"] = {
                "mode": "generated-local",
                "realms": [
                    {
                        "name": "dp-p1",
                        "roles": [],
                        "clients": [
                            {"client_id": "driverplus-mobile-app", "kind": "public"}
                        ],
                    },
                    {
                        "name": "dp-p2",
                        "roles": ["P2d"],
                        "clients": [
                            {"client_id": "ops-console-web", "kind": "public"},
                            {
                                "client_id": "d-taxonomy",
                                "kind": "public",
                                "roles": ["taxonomy-reader"],
                            },
                            {
                                "client_id": "identity-admin",
                                "kind": "service-account",
                                "secret_env": "KEYCLOAK_ADMIN_CLIENT_SECRET",
                            },
                        ],
                    },
                ],
                "seed_users": [],
            }
            with (
                mock.patch.object(INFRA, "_port_is_available", return_value=True),
                mock.patch("builtins.print"),
            ):
                plan = INFRA.build_plan(project, False, config=config)
                INFRA.write_plan(plan)

            infra = project / "infra"
            apps = (infra / "docker-compose.apps.yml").read_text()
            shared = (infra / "docker-compose.infra.yml").read_text()
            taxonomy_dockerfile = (
                infra / "dockerfiles" / "Dockerfile.d-taxonomy"
            ).read_text()
            identity_dockerfile = (
                infra / "dockerfiles" / "Dockerfile.d-identity-trust"
            ).read_text()
            pg = (infra / "pg-init" / "00-multi-db.sh").read_text()
            kafka = (infra / "provision" / "kafka.sh").read_text()
            env_contract = json.loads((infra / "contracts" / "env.json").read_text())
            keycloak_contract = json.loads(
                (infra / "contracts" / "keycloak.json").read_text()
            )

            self.assertIn("REDIS_HOST: redis", apps)
            self.assertIn("REDIS_PORT: 6379", apps)
            self.assertNotIn("REDIS_URL", apps)
            self.assertIn("http://d-identity-trust:3000", apps)
            self.assertIn("http://localhost:3010", apps)
            self.assertIn("http://keycloak:8080/realms/dp-p1", apps)
            self.assertIn("${KEYCLOAK_ADMIN_CLIENT_SECRET:?", apps)
            self.assertIn("profiles: [migrate]", apps)
            self.assertIn("command: [npx, prisma, migrate, deploy]", apps)

            self.assertLess(
                taxonomy_dockerfile.index("COPY prisma ./prisma"),
                taxonomy_dockerfile.index("npm ci"),
            )
            self.assertIn("FROM node:24-slim", taxonomy_dockerfile)
            self.assertIn("openssl ca-certificates", taxonomy_dockerfile)
            self.assertIn("npx prisma generate && npm run build", taxonomy_dockerfile)
            self.assertIn('CMD ["npm", "run", "prod"]', taxonomy_dockerfile)
            self.assertIn("/health/ready", taxonomy_dockerfile)
            self.assertNotIn("prisma migrate deploy", taxonomy_dockerfile)
            self.assertNotIn("COPY prisma ./prisma", identity_dockerfile)

            self.assertIn("ensure_schema d-taxonomy taxonomy d-taxonomy", pg)
            self.assertIn("ensure_schema d-taxonomy taxonomy_audit d-taxonomy", pg)
            self.assertIn("SKIP_DB_CREATE: 'true'", shared)
            self.assertIn("postgres-provision:", shared)
            self.assertIn("keycloak-provision:", shared)
            self.assertIn("kafka-provision:", shared)
            self.assertNotIn("--import-realm", shared)
            self.assertIn("identity.user.profile.registered.v1", kafka)
            self.assertIn("notification.dispatch.requested.high.v1", kafka)
            self.assertIn("auto_create_topics_enabled false", kafka)
            taxonomy_contract = env_contract["services"]["d-taxonomy"]
            self.assertIn("REDIS_HOST", taxonomy_contract["required_keys"])
            self.assertIn("DATABASE_URL", taxonomy_contract["required_keys"])
            self.assertNotIn("REDIS_URL", taxonomy_contract["required_keys"])
            taxonomy_client = next(
                client
                for realm in keycloak_contract["realms"]
                for client in realm["clients"]
                if client["client_id"] == "d-taxonomy"
            )
            self.assertEqual(taxonomy_client["roles"], ["taxonomy-reader"])

    def test_shared_database_requires_one_reviewed_owner_and_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            for index, name in enumerate(("service-a", "service-b")):
                service = project / name
                service.mkdir()
                (service / "package.json").write_text(json.dumps({
                    "scripts": {"build": "echo build", "prod": "node dist/main.js"},
                    "dependencies": {"pg": "8.0.0"},
                }))
                (service / ".env.example").write_text(
                    f"PORT={4100 + index}\nDATABASE_URL=postgresql://local@localhost/db\n"
                )
            with mock.patch.object(INFRA, "_port_is_available", return_value=True):
                config = INFRA.detection_report(project)["suggested_config"]
            for name in ("service-a", "service-b"):
                config["services"][name]["database"].update({
                    "name": "shared",
                    "password_env": "SHARED_DB_PASSWORD",
                })
            config["services"]["service-a"]["database"]["owner"] = "owner-a"
            config["services"]["service-b"]["database"]["owner"] = "owner-b"
            with (
                mock.patch.object(INFRA, "_port_is_available", return_value=True),
                mock.patch("builtins.print"),
                self.assertRaises(SystemExit) as stopped,
            ):
                INFRA.build_plan(project, False, config=config)
            self.assertEqual(stopped.exception.code, 2)

            config["services"]["service-b"]["database"]["owner"] = "owner-a"
            with (
                mock.patch.object(INFRA, "_port_is_available", return_value=True),
                mock.patch("builtins.print"),
            ):
                plan = INFRA.build_plan(project, False, config=config)
            self.assertEqual({service.db_name for service in plan.services}, {"shared"})
            self.assertIn("SHARED_DB_PASSWORD", INFRA.render_env_example(plan))


class ProvisionerExecutionTests(unittest.TestCase):
    def test_postgres_reconciler_is_repeatable_and_keeps_password_out_of_argv(self) -> None:
        service = INFRA.ServiceCandidate(
            name="app",
            path=Path("/unused/app"),
            detected_infra={"postgres"},
            db_name="app-db",
            db_role="app-role",
            db_password_env="APP_DB_PASSWORD",
            db_schemas=["app", "audit"],
        )
        plan = INFRA.InitPlan(
            project_root=Path("/unused"),
            project_name="provision-test",
            network_name="provision-test-net",
            services=[service],
            infra_modules=["postgres"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            state = root / "state"
            fake_bin.mkdir()
            state.mkdir()
            script = root / "postgres.sh"
            script.write_text(INFRA.render_pg_init(plan))
            script.chmod(0o755)
            self._write_executable(
                fake_bin / "psql",
                "#!/usr/bin/env bash\n"
                "printf 'psql %s\\n' \"$*\" >>\"$PROVISION_LOG\"\n"
                "db='' query=0\n"
                "for arg in \"$@\"; do\n"
                "  case \"$arg\" in\n"
                "    --set=db_name=*) db=${arg#--set=db_name=} ;;\n"
                "    '--command=SELECT 1 FROM pg_database'*) query=1 ;;\n"
                "  esac\n"
                "done\n"
                "[ \"$query\" = 1 ] && [ -f \"$PG_STATE/$db\" ] && printf '1\\n'\n"
                "exit 0\n",
            )
            self._write_executable(
                fake_bin / "createdb",
                "#!/usr/bin/env bash\n"
                "printf 'createdb %s\\n' \"$*\" >>\"$PROVISION_LOG\"\n"
                "database=${!#}\n"
                "touch \"$PG_STATE/$database\"\n",
            )
            log = root / "provision.log"
            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                "PROVISION_LOG": str(log),
                "PG_STATE": str(state),
                "APP_DB_PASSWORD": "do-not-leak-this-password",
            })

            first = subprocess.run(
                [str(script)], env=env, capture_output=True, text=True, check=False
            )
            second = subprocess.run(
                [str(script)], env=env, capture_output=True, text=True, check=False
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            commands = log.read_text()
            self.assertEqual(commands.count("createdb --owner=app-role app-db"), 1)
            self.assertEqual(commands.count("--set=schema_name=app"), 2)
            self.assertEqual(commands.count("--set=schema_name=audit"), 2)
            self.assertNotIn("do-not-leak-this-password", commands)

    def test_kafka_reconciler_executes_an_idempotent_reviewed_contract_twice(self) -> None:
        plan = INFRA.InitPlan(
            project_root=Path("/unused"),
            project_name="provision-test",
            network_name="provision-test-net",
            services=[],
            infra_modules=["kafka"],
            kafka_spec={
                "strict": True,
                "topics": [{
                    "name": "audit.events.v1",
                    "partitions": 2,
                    "replication_factor": 1,
                    "config": {"cleanup.policy": "compact"},
                }],
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "rpk.log"
            script = root / "kafka.sh"
            script.write_text(INFRA.render_kafka_provisioner(plan))
            script.chmod(0o755)
            self._write_executable(
                fake_bin / "rpk",
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >>\"$RPK_LOG\"\n"
                "case \"$*\" in\n"
                "  '-X brokers=redpanda:29092 topic describe audit.events.v1')\n"
                "    [ -f \"$RPK_STATE/topic\" ] || exit 1\n"
                "    printf 'SUMMARY\\n=======\\nNAME             PARTITIONS  REPLICAS\\naudit.events.v1  2           1\\n' ;;\n"
                "  '-X brokers=redpanda:29092 topic create audit.events.v1 --partitions 2 --replicas 1') touch \"$RPK_STATE/topic\" ;;\n"
                "  '-X brokers=redpanda:29092 topic alter-config audit.events.v1 --set cleanup.policy=compact') ;;\n"
                "  '-X brokers=redpanda:29092 cluster config set auto_create_topics_enabled false') ;;\n"
                "  *) exit 9 ;;\n"
                "esac\n",
            )
            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                "RPK_LOG": str(log),
                "RPK_STATE": str(root),
            })

            first = subprocess.run(
                [str(script)], env=env, capture_output=True, text=True, check=False
            )
            second = subprocess.run(
                [str(script)], env=env, capture_output=True, text=True, check=False
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            commands = log.read_text().splitlines()
            self.assertEqual(len(commands), 10)
            self.assertEqual(sum(" topic create " in line for line in commands), 1)
            self.assertFalse(any("--if-not-exists" in line for line in commands))
            self.assertEqual(
                sum("auto_create_topics_enabled false" in line for line in commands), 2
            )

    def test_keycloak_reconciler_creates_then_updates_the_same_local_contract(self) -> None:
        state = {
            "realms": set(),
            "realm_roles": set(),
            "clients": {},
            "client_roles": set(),
        }

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, payload=None) -> None:
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _payload(self):
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if path == "/admin/realms/local":
                    self._send(200, {"realm": "local"}) if "local" in state["realms"] else self._send(404)
                elif path == "/admin/realms/local/roles/developer":
                    self._send(200, {"name": "developer"}) if "developer" in state["realm_roles"] else self._send(404)
                elif path == "/admin/realms/local/clients":
                    client_id = urllib.parse.parse_qs(parsed.query).get("clientId", [""])[0]
                    client = state["clients"].get(client_id)
                    self._send(200, [client] if client else [])
                elif path == "/admin/realms/local/clients/client-web/roles/reader":
                    self._send(200, {"name": "reader"}) if "reader" in state["client_roles"] else self._send(404)
                else:
                    self._send(500, {"unexpected": path})

            def do_POST(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if path == "/realms/master/protocol/openid-connect/token":
                    self._send(200, {"access_token": "admin-token"})
                elif path == "/admin/realms":
                    state["realms"].add(self._payload()["realm"])
                    self._send(201)
                elif path == "/admin/realms/local/roles":
                    state["realm_roles"].add(self._payload()["name"])
                    self._send(201)
                elif path == "/admin/realms/local/clients":
                    client_id = self._payload()["clientId"]
                    state["clients"][client_id] = {"id": "client-web", "clientId": client_id}
                    self._send(201)
                elif path == "/admin/realms/local/clients/client-web/roles":
                    state["client_roles"].add(self._payload()["name"])
                    self._send(201)
                else:
                    self._send(500, {"unexpected": path})

            def do_PUT(self):  # noqa: N802
                if self.path in {
                    "/admin/realms/local",
                    "/admin/realms/local/clients/client-web",
                }:
                    self._payload()
                    self._send(204)
                else:
                    self._send(500, {"unexpected": self.path})

            def log_message(self, format, *args):  # noqa: A003
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract = root / "keycloak.json"
            contract.write_text(json.dumps({
                "mode": "generated-local",
                "realms": [{
                    "name": "local",
                    "roles": ["developer"],
                    "clients": [{
                        "client_id": "web-app",
                        "kind": "public",
                        "roles": ["reader"],
                    }],
                }],
                "seed_users": [],
            }))
            script = root / "keycloak.py"
            script.write_text(INFRA.render_keycloak_provisioner())
            env = os.environ.copy()
            env.update({
                "KEYCLOAK_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "KEYCLOAK_CONTRACT_PATH": str(contract),
                "KEYCLOAK_ADMIN": "admin",
                "KEYCLOAK_ADMIN_PASSWORD": "admin-secret",
            })

            first = subprocess.run(
                [sys.executable, str(script)], env=env, capture_output=True, text=True, check=False
            )
            second = subprocess.run(
                [sys.executable, str(script)], env=env, capture_output=True, text=True, check=False
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("created realm local", first.stdout)
            self.assertIn("reconciled realm local", second.stdout)
            self.assertEqual(set(state["clients"]), {"web-app"})
            self.assertEqual(state["realm_roles"], {"developer"})
            self.assertEqual(state["client_roles"], {"reader"})

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content)
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
