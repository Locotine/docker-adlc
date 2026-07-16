from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INFRA_INIT = ROOT / "scripts" / "infra-init.py"


def load_infra_init_module():
    spec = importlib.util.spec_from_file_location("docker_claude_infra_init", INFRA_INIT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load infra init module from {INFRA_INIT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INFRA_INIT_MODULE = load_infra_init_module()


class InfraInitCredentialTests(unittest.TestCase):
    def make_plan(self):
        audit = INFRA_INIT_MODULE.ServiceAudit(
            name="orders-api",
            prisma=True,
            env={
                "DATABASE_URL": INFRA_INIT_MODULE.EnvEvidence(
                    key="DATABASE_URL", required=True, sources={"prisma:schema.prisma:env"}
                ),
                "STRIPE_API_KEY": INFRA_INIT_MODULE.EnvEvidence(
                    key="STRIPE_API_KEY", required=True, secret=True,
                    sources={"source:billing.ts:getOrThrow"},
                ),
            },
        )
        service = INFRA_INIT_MODULE.ServiceCandidate(
            name="orders-api",
            path=ROOT / "orders-api",
            detected_infra={"postgres", "minio"},
            host_port=4000,
            container_port=3000,
            audit=audit,
            env_values={"DATABASE_URL": "", "STRIPE_API_KEY": None},
            secret_keys={"STRIPE_API_KEY"},
            start_script="prod",
            migration_mode="auto",
        )
        return INFRA_INIT_MODULE.InitPlan(
            project_root=ROOT,
            project_name="test-project",
            network_name="test-network",
            services=[service],
            infra_modules=["postgres", "keycloak", "temporal", "minio"],
        )

    def test_compose_uses_required_secret_variables(self) -> None:
        plan = self.make_plan()

        infra_yaml = INFRA_INIT_MODULE.render_infra_yaml(plan)
        apps_yaml = INFRA_INIT_MODULE.render_apps_yaml(plan)

        self.assertIn("${POSTGRES_PASSWORD:?", infra_yaml)
        self.assertIn("${ORDERS_API_DB_PASSWORD:?", infra_yaml)
        self.assertIn("${KEYCLOAK_ADMIN_PASSWORD:?", infra_yaml)
        self.assertIn("${KEYCLOAK_DB_PASSWORD:?", infra_yaml)
        self.assertIn("${TEMPORAL_DB_PASSWORD:?", infra_yaml)
        self.assertIn("${MINIO_ROOT_PASSWORD:?", infra_yaml)
        self.assertIn("${ORDERS_API_DB_PASSWORD_URLENCODED:?", apps_yaml)
        self.assertNotIn("${ORDERS_API_DB_PASSWORD:?", apps_yaml)
        self.assertNotIn("orders-api:orders-api@", apps_yaml)
        self.assertNotIn("POSTGRES_PASSWORD: postgres", infra_yaml)
        self.assertNotIn("MINIO_ROOT_PASSWORD: minioadmin", infra_yaml)

    def test_pg_init_reads_passwords_from_environment(self) -> None:
        script = INFRA_INIT_MODULE.render_pg_init(self.make_plan())

        self.assertIn('ensure_role_and_db orders-api ORDERS_API_DB_PASSWORD orders-api', script)
        self.assertIn('local password="${!password_var:?set $password_var}"', script)
        self.assertIn("CREATE ROLE %I LOGIN PASSWORD %L", script)
        self.assertNotIn("WITH PASSWORD 'orders-api'", script)
        self.assertNotIn("WITH PASSWORD 'keycloak'", script)
        self.assertNotIn("WITH PASSWORD 'temporal'", script)

    def test_env_example_lists_every_required_secret(self) -> None:
        env_example = INFRA_INIT_MODULE.render_env_example(self.make_plan())

        for key in (
            "POSTGRES_PASSWORD",
            "ORDERS_API_DB_PASSWORD",
            "KEYCLOAK_ADMIN_PASSWORD",
            "KEYCLOAK_DB_PASSWORD",
            "TEMPORAL_DB_PASSWORD",
            "MINIO_ROOT_USER",
            "MINIO_ROOT_PASSWORD",
        ):
            self.assertIn(f"{key}=GENERATE_ME_", env_example)
        self.assertIn("STRIPE_API_KEY=REPLACE_ME_STRIPE_API_KEY", env_example)
        self.assertNotIn("STRIPE_API_KEY=GENERATE_ME_", env_example)


if __name__ == "__main__":
    unittest.main()
