#!/usr/bin/env python3
"""infra-init — scaffold shared Docker infra for a multi-service project.

Interactive or non-interactive workflow:
  1. Detect candidate service folders (dir với package.json / go.mod / pyproject.toml).
  2. Parse tech-stack hints (deps in package.json, imports) to guess needed infra:
        prisma|pg|typeorm         → postgres
        redis|ioredis|bullmq       → redis
        kafkajs|@confluentinc      → redpanda (kafka)
        openid-client|jose|passport-jwt|keycloak-* → keycloak
        @temporalio/*              → temporal
        @aws-sdk/client-s3|minio   → minio
        @elastic/elasticsearch     → elasticsearch
  3. Prompt user to confirm services, port mapping, compose project name, or use
     detected defaults with --yes.
  4. Prompt to enable/disable each detected infra module, or enable detected
     modules with --yes.
  5. Generate `infra/` next to the services:
        infra/docker-compose.infra.yml
        infra/docker-compose.apps.yml
        infra/dockerfiles/Dockerfile.<service>          (only for Node services without Dockerfile)
        infra/.env.example
        infra/.gitignore
        infra/README.md
        infra/pg-init/00-multi-db.sh                     (if postgres)
        infra/contracts/{env,postgres,keycloak,kafka}.json
        infra/provision/{keycloak.py,kafka.sh}           (when enabled)

Not destructive by default — refuses to overwrite existing infra/ unless `--force`.
Prints a diff-friendly plan before writing.

Requires: Python 3.8+. No external deps.
"""

from __future__ import annotations

import argparse
import errno
import json
import re
import shlex
import shutil
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from docker_contract import (  # noqa: E402
    ENV_KEY_RE,
    TOPIC_RE,
    EnvEvidence,
    ServiceAudit,
    audit_service,
    is_placeholder,
    is_secret_key,
)

# ---------- Dependency → infra module map ----------

DEP_HINTS: Dict[str, str] = {
    "prisma": "postgres",
    "@prisma/client": "postgres",
    "pg": "postgres",
    "typeorm": "postgres",
    "sequelize": "postgres",
    "kysely": "postgres",
    "redis": "redis",
    "ioredis": "redis",
    "bullmq": "redis",
    "cache-manager-ioredis": "redis",
    "cache-manager-redis-store": "redis",
    "kafkajs": "kafka",
    "@confluentinc/kafka-javascript": "kafka",
    "@nestjs/microservices": "kafka",  # ambiguous but common
    "openid-client": "keycloak",
    "jose": "keycloak",
    "passport-jwt": "keycloak",
    "keycloak-connect": "keycloak",
    "keycloak-admin-client": "keycloak",
    "@temporalio/client": "temporal",
    "@temporalio/worker": "temporal",
    "@temporalio/workflow": "temporal",
    "@aws-sdk/client-s3": "minio",
    "minio": "minio",
    "@elastic/elasticsearch": "elasticsearch",
}

# Python / Go hints (bonus)
PY_HINTS = {
    "psycopg2": "postgres", "psycopg2-binary": "postgres", "asyncpg": "postgres", "sqlalchemy": "postgres",
    "redis": "redis", "aioredis": "redis",
    "confluent-kafka": "kafka", "aiokafka": "kafka",
    "python-keycloak": "keycloak", "authlib": "keycloak",
    "temporalio": "temporal",
    "boto3": "minio", "minio": "minio",
    "elasticsearch": "elasticsearch",
}


# ---------- Data model ----------

@dataclass
class ServiceCandidate:
    name: str
    path: Path
    language: str = "unknown"   # node|python|go|unknown
    deps: Set[str] = field(default_factory=set)
    has_dockerfile: bool = False
    detected_port: Optional[int] = None
    detected_infra: Set[str] = field(default_factory=set)
    dockerfile_source: str = "generated"  # service|generated
    audit: Optional[ServiceAudit] = None
    env_values: Dict[str, Optional[str]] = field(default_factory=dict)
    secret_keys: Set[str] = field(default_factory=set)
    generated_secret_keys: Set[str] = field(default_factory=set)
    service_refs: Dict[str, str] = field(default_factory=dict)
    health_path: Optional[str] = None
    start_script: Optional[str] = None
    build_script: Optional[str] = None
    base_image: str = "node:24-slim"
    migration_mode: str = "disabled"  # auto|disabled
    db_schemas: List[str] = field(default_factory=list)
    db_name: Optional[str] = None
    db_role: Optional[str] = None
    db_password_env: Optional[str] = None
    # Chosen by user:
    include: bool = True
    host_port: Optional[int] = None
    container_port: int = 3000


@dataclass
class InitPlan:
    project_root: Path
    project_name: str                 # compose `name:`
    network_name: str
    services: List[ServiceCandidate]
    infra_modules: List[str]
    infra_ports: Dict[str, Dict[str, int]] = field(default_factory=dict)
    keycloak_spec: Dict[str, Any] = field(default_factory=dict)
    kafka_spec: Dict[str, Any] = field(default_factory=dict)
    force: bool = False


# ---------- Discovery ----------

IGNORE_DIRS = {"node_modules", ".git", "dist", "build", "coverage", ".next", ".turbo", "infra", "scripts", ".claude"}


def scan_candidates(root: Path) -> List[ServiceCandidate]:
    out: List[ServiceCandidate] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in IGNORE_DIRS or child.name.startswith("."):
            continue
        pkg = child / "package.json"
        pyproj = child / "pyproject.toml"
        reqs = child / "requirements.txt"
        gomod = child / "go.mod"
        if not (pkg.exists() or pyproj.exists() or reqs.exists() or gomod.exists()):
            continue
        s = ServiceCandidate(name=child.name, path=child)
        if pkg.exists():
            s.language = "node"
            try:
                data = json.loads(pkg.read_text())
                deps = {}
                deps.update(data.get("dependencies") or {})
                deps.update(data.get("devDependencies") or {})
                s.deps = set(deps.keys())
                for d in s.deps:
                    hint = DEP_HINTS.get(d)
                    if hint:
                        s.detected_infra.add(hint)
                # Port hint from scripts or .env.example
                s.detected_port = _guess_port(child)
            except Exception:
                pass
        elif pyproj.exists() or reqs.exists():
            s.language = "python"
            content = ""
            if pyproj.exists():
                content = pyproj.read_text()
            if reqs.exists():
                content += "\n" + reqs.read_text()
            for lib, mod in PY_HINTS.items():
                if re.search(rf"\b{re.escape(lib)}\b", content):
                    s.deps.add(lib)
                    s.detected_infra.add(mod)
        elif gomod.exists():
            s.language = "go"
            # rudimentary: look at go.sum imports too if available
        s.has_dockerfile = (child / "Dockerfile").exists()
        s.dockerfile_source = "service" if s.has_dockerfile else "generated"
        s.audit = audit_service(child, s.name)
        s.start_script = s.audit.start_script
        s.build_script = s.audit.build_script
        s.health_path = s.audit.health_candidates[0] if s.audit.health_candidates else None
        s.base_image = f"node:{s.audit.node_major}-slim"
        s.migration_mode = "auto" if s.audit.prisma else "disabled"
        s.db_schemas = list(s.audit.prisma_schemas)
        s.db_name = s.name
        s.db_role = s.name
        s.db_password_env = _service_db_password_key(s.name)
        for key in s.audit.env:
            prefix = key.upper()
            if prefix.startswith("REDIS_"):
                s.detected_infra.add("redis")
            if prefix.startswith("KAFKA_"):
                s.detected_infra.add("kafka")
            if prefix.startswith("KEYCLOAK_"):
                s.detected_infra.add("keycloak")
            if prefix.startswith("TEMPORAL_"):
                s.detected_infra.add("temporal")
            if prefix.startswith(("DATABASE_", "POSTGRES_")):
                s.detected_infra.add("postgres")
        if s.audit.prisma:
            s.detected_infra.add("postgres")
        if s.audit.topics:
            s.detected_infra.add("kafka")
        out.append(s)
    return out


PORT_ENV_RE = re.compile(r"^\s*PORT\s*=\s*(\d+)\s*$", re.MULTILINE)


def _guess_port(svc: Path) -> Optional[int]:
    for name in (".env.example", ".env"):
        p = svc / name
        if p.exists():
            m = PORT_ENV_RE.search(p.read_text())
            if m:
                return int(m.group(1))
    return None


# ---------- Interactive prompts ----------

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            print()
            sys.exit(1)
        return raw or default


def ask_yn(prompt: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    r = ask(f"{prompt} ({d})").lower()
    if not r:
        return default_yes
    return r.startswith("y")


def ask_int(prompt: str, default: int) -> int:
    while True:
        r = ask(prompt, str(default))
        try:
            return int(r)
        except ValueError:
            print("  not a number, try again")


# ---------- Plan & templates ----------

INFRA_MODULES_ALL = ["postgres", "redis", "kafka", "keycloak", "temporal", "minio", "elasticsearch"]

# These dependencies are useful hints, but do not prove that the module is used
# in this particular service. Agents should confirm them during preflight.
AMBIGUOUS_DEP_HINTS = {
    "@nestjs/microservices": "kafka",
}

# Internal hostname each module uses (referenced from apps compose)
MODULE_HOST = {
    "postgres": "postgres",
    "redis": "redis",
    "kafka": "redpanda",
    "keycloak": "keycloak",
    "temporal": "temporal",
    "minio": "minio",
    "elasticsearch": "elasticsearch",
}

# endpoint -> (container port, preferred host port). Endpoint keys are stable
# config names; they are not tied to any particular project or service folder.
INFRA_PORT_SPECS: Dict[str, Dict[str, Tuple[int, int]]] = {
    "postgres": {"postgres": (5432, 5432)},
    "redis": {"redis": (6379, 6379)},
    "kafka": {
        "kafka": (9092, 9092),
        "kafka_proxy": (8082, 8082),
        "kafka_admin": (9644, 9644),
    },
    "keycloak": {"keycloak": (8080, 8080)},
    "temporal": {
        "temporal": (7233, 7233),
        "temporal_ui": (8080, 8233),
    },
    "minio": {
        "minio_api": (9000, 9000),
        "minio_console": (9001, 9001),
    },
    "elasticsearch": {"elasticsearch": (9200, 9200)},
}

# Compose service keys emitted by each infra module. App services share the
# same merged Compose model, so these names must never overlap.
INFRA_SERVICE_KEYS: Dict[str, Set[str]] = {
    "postgres": {"postgres"},
    "redis": {"redis"},
    "kafka": {"redpanda"},
    "keycloak": {"keycloak"},
    "temporal": {"temporal", "temporal-ui"},
    "minio": {"minio"},
    "elasticsearch": {"elasticsearch"},
}

COMPOSE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
COMPOSE_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _normalize_compose_name(value: str, fallback: str = "project") -> str:
    """Return a Docker Compose project name accepted by the Docker CLI."""
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    normalized = normalized.strip("-_")
    return normalized or fallback


def _normalize_image_component(value: str, fallback: str = "app") -> str:
    """Return a lowercase Docker repository path component."""
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower())
    normalized = re.sub(r"[._-]{2,}", "-", normalized).strip("._-")
    return normalized or fallback


def _service_name_is_safe(value: str) -> bool:
    return COMPOSE_SERVICE_RE.fullmatch(value) is not None


def _image_name_collisions(services: List[ServiceCandidate]) -> Dict[str, List[str]]:
    owners: Dict[str, List[str]] = {}
    for service in services:
        owners.setdefault(_normalize_image_component(service.name), []).append(service.name)
    return {image: names for image, names in owners.items() if len(names) > 1}


def _infra_service_names(modules: List[str]) -> Set[str]:
    return {
        service_name.casefold()
        for module in modules
        for service_name in INFRA_SERVICE_KEYS[module]
    }


def _dockerfile_has_fixed_identity(path: Path) -> bool:
    """Detect fixed numeric UID/GID creation that may collide with a base image."""
    try:
        content = path.read_text()
    except (OSError, UnicodeError):
        return False
    logical_content = re.sub(r"\\\s*\n\s*", " ", content)
    identity_command = r"(?:groupadd|addgroup|useradd|adduser)"
    numeric_flag = r"(?:(?:--(?:gid|uid))(?:=|\s+)|-[gu]\s+)\d+"
    if re.search(
        rf"\b{identity_command}\b[^\n]*{numeric_flag}",
        logical_content,
        re.IGNORECASE,
    ):
        return True
    fixed_args = re.findall(
        r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\d+\s*$",
        content,
        re.MULTILINE | re.IGNORECASE,
    )
    return any(
        re.search(
            rf"\b{identity_command}\b[^\n]*\$(?:\{{{re.escape(arg)}\}}|{re.escape(arg)}\b)",
            logical_content,
            re.IGNORECASE,
        )
        for arg in fixed_args
    )


def _port_is_available(port: int) -> bool:
    """Best-effort check that Docker can publish a TCP port on all interfaces."""
    checks = ((socket.AF_INET, "0.0.0.0"),)
    if socket.has_ipv6:
        checks += ((socket.AF_INET6, "::"),)
    for family, host in checks:
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            if family == socket.AF_INET6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe.bind((host, port))
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in {
                errno.EAFNOSUPPORT,
                errno.EADDRNOTAVAIL,
            }:
                continue
            return False
        finally:
            probe.close()
    return True


def _next_available_host_port(preferred: int, reserved: Set[int]) -> int:
    candidate = preferred
    while candidate <= 65535:
        if candidate not in reserved and _port_is_available(candidate):
            return candidate
        candidate += 1
    candidate = 1024
    while candidate < preferred:
        if candidate not in reserved and _port_is_available(candidate):
            return candidate
        candidate += 1
    raise RuntimeError("no available TCP host port found")


def _default_modules(cands: List[ServiceCandidate]) -> List[str]:
    aggregate: Set[str] = set()
    for service in cands:
        aggregate.update(service.detected_infra)
    if aggregate.intersection({"keycloak", "temporal"}):
        aggregate.add("postgres")
    return [module for module in INFRA_MODULES_ALL if module in aggregate]


def _default_ports(
    cands: List[ServiceCandidate],
    reserved: Optional[Set[int]] = None,
) -> Dict[str, Tuple[int, int]]:
    ports: Dict[str, Tuple[int, int]] = {}
    used_host_ports: Set[int] = set(reserved or set())
    next_port = 4000
    for service in cands:
        preferred = service.detected_port or next_port
        host_port = _next_available_host_port(preferred, used_host_ports)
        container_port = service.detected_port or 3000
        ports[service.name] = (host_port, container_port)
        used_host_ports.add(host_port)
        next_port = host_port + 1
    return ports


def _default_infra_ports(
    modules: List[str],
    reserved: Optional[Set[int]] = None,
) -> Dict[str, Dict[str, int]]:
    used_host_ports: Set[int] = set(reserved or set())
    result: Dict[str, Dict[str, int]] = {}
    for module in modules:
        result[module] = {}
        for endpoint, (_, preferred_host_port) in INFRA_PORT_SPECS[module].items():
            host_port = _next_available_host_port(preferred_host_port, used_host_ports)
            result[module][endpoint] = host_port
            used_host_ports.add(host_port)
    return result


def _default_service_contract(service: ServiceCandidate) -> Dict[str, Any]:
    audit = service.audit or ServiceAudit(name=service.name)
    env = {
        key: item.value
        for key, item in audit.env.items()
        if item.value is not None
    }
    secret_keys = sorted(key for key, item in audit.env.items() if item.secret)
    return {
        "env": env,
        "secret_keys": secret_keys,
        # App credentials are external by default.  A reviewed plan may opt a
        # key into local random generation explicitly.
        "generated_secret_keys": [],
        "service_refs": {},
        "health_path": service.health_path,
        "start_script": service.start_script,
        "build_script": service.build_script,
        "base_image": service.base_image,
        "migration": {"mode": service.migration_mode},
        "database": {
            "name": service.db_name or service.name,
            "owner": service.db_role or service.name,
            "password_env": service.db_password_env or _service_db_password_key(service.name),
            "schemas": list(service.db_schemas),
        },
        "detected_contract": audit.to_dict(),
    }


def _key_suffix(key: str, marker: str) -> str:
    _, _, suffix = key.partition(marker)
    return suffix.strip("_")


def _default_keycloak_spec(services: List[ServiceCandidate]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    realms: Dict[str, Dict[str, Any]] = {}
    realm_suffixes: Dict[str, str] = {}
    pending_roles: List[Tuple[str, str]] = []
    pending_client_roles: List[Tuple[str, str, str, str]] = []
    pending_clients: List[Dict[str, Any]] = []
    uncertainties: List[Dict[str, Any]] = []

    for service in services:
        audit = service.audit or ServiceAudit(name=service.name)
        audiences = [
            (key, item.value)
            for key, item in audit.env.items()
            if "AUDIENCE" in key and item.value and not is_placeholder(item.value)
        ]
        for key, item in audit.env.items():
            value = item.value
            if not value or is_placeholder(value):
                continue
            if "REALM" in key and re.fullmatch(r"[A-Za-z0-9._-]+", value):
                realms.setdefault(value, {"name": value, "roles": [], "clients": []})
                realm_suffixes[_key_suffix(key, "REALM")] = value
            for match in re.findall(r"/realms/([^/\s]+)", value):
                realms.setdefault(match, {"name": match, "roles": [], "clients": []})

    def resolve_realm(key: str) -> Optional[str]:
        matching = [
            realm
            for suffix, realm in realm_suffixes.items()
            if suffix and (key.endswith(suffix) or suffix.endswith(_key_suffix(key, "AUDIENCE")))
        ]
        if len(set(matching)) == 1:
            return matching[0]
        if len(realms) == 1:
            return next(iter(realms))
        return None

    for service in services:
        audit = service.audit or ServiceAudit(name=service.name)
        for key, item in audit.env.items():
            value = item.value
            if not value or is_placeholder(value):
                continue
            if "AUDIENCE" in key:
                pending_clients.append({
                    "client_id": value,
                    "realm": resolve_realm(key),
                    "kind": "public",
                    "roles": [],
                    "source": f"{service.name}:{key}",
                })
            if "ROLE" in key and audit.realm_access_claim:
                pending_roles.append((value, resolve_realm(key) or ""))
            if "ROLE" in key and audit.resource_access_claim:
                if len(audiences) == 1:
                    audience_key, client_id = audiences[0]
                    pending_client_roles.append((
                        client_id or "",
                        value,
                        resolve_realm(audience_key) or "",
                        service.name,
                    ))
                else:
                    uncertainties.append({
                        "type": "keycloak_client_role_owner_ambiguous",
                        "role": value,
                        "service": service.name,
                        "question": (
                            f"Which Keycloak resource client owns client role {value!r} "
                            f"read by {service.name}?"
                        ),
                    })
            if key.endswith("_CLIENT_ID"):
                secret_key = key[:-len("_CLIENT_ID")] + "_CLIENT_SECRET"
                if secret_key in audit.env:
                    pending_clients.append({
                        "client_id": value,
                        "realm": resolve_realm(key),
                        "kind": "service-account",
                        "roles": [],
                        "secret_env": secret_key,
                        "source": f"{service.name}:{key}",
                    })

    for role, realm in pending_roles:
        if realm:
            realms[realm]["roles"].append(role)
        else:
            uncertainties.append({
                "type": "keycloak_role_realm_ambiguous",
                "role": role,
                "question": f"Which detected Keycloak realm owns realm role {role!r}?",
            })
    for client in pending_clients:
        realm = client.pop("realm")
        if realm:
            realms[realm]["clients"].append(client)
        else:
            uncertainties.append({
                "type": "keycloak_client_realm_ambiguous",
                "client": client["client_id"],
                "source": client["source"],
                "question": (
                    f"Which detected Keycloak realm owns client {client['client_id']!r}?"
                ),
            })
    for client_id, role, realm, service_name in pending_client_roles:
        if not realm:
            uncertainties.append({
                "type": "keycloak_client_role_realm_ambiguous",
                "client": client_id,
                "role": role,
                "service": service_name,
                "question": (
                    f"Which detected realm owns client role {client_id}/{role}?"
                ),
            })
            continue
        matching = [
            client for client in realms[realm]["clients"]
            if client["client_id"] == client_id
        ]
        if matching:
            matching[0].setdefault("roles", []).append(role)
        else:
            uncertainties.append({
                "type": "keycloak_client_role_owner_ambiguous",
                "client": client_id,
                "role": role,
                "service": service_name,
                "question": f"Add client {client_id!r} to realm {realm!r} for role {role!r}?",
            })
    for realm in realms.values():
        realm["roles"] = sorted(set(realm["roles"]))
        unique_clients: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for client in realm["clients"]:
            key = (client["client_id"], client["kind"])
            if key in unique_clients:
                unique_clients[key].setdefault("roles", []).extend(client.get("roles", []))
            else:
                unique_clients[key] = client
        for client in unique_clients.values():
            client["roles"] = sorted(set(client.get("roles", [])))
        realm["clients"] = sorted(unique_clients.values(), key=lambda item: item["client_id"])
    return {
        "mode": "generated-local",
        "realms": sorted(realms.values(), key=lambda item: item["name"]),
        "seed_users": [],
    }, uncertainties


def _default_kafka_spec(services: List[ServiceCandidate]) -> Dict[str, Any]:
    topics = sorted({topic for service in services for topic in (service.audit.topics if service.audit else [])})
    return {
        "strict": bool(topics),
        "topics": [
            {"name": topic, "partitions": 1, "replication_factor": 1, "config": {}}
            for topic in topics
        ],
    }


def _contract_uncertainties(service: ServiceCandidate) -> List[Dict[str, Any]]:
    audit = service.audit or ServiceAudit(name=service.name)
    uncertainties: List[Dict[str, Any]] = []
    drift = sorted(audit.code_env_keys.difference(audit.env_example_keys))
    if drift:
        uncertainties.append({
            "type": "env_contract_drift",
            "service": service.name,
            "keys": drift,
            "question": (
                f"{service.name} reads env keys absent from .env.example: {', '.join(drift)}. "
                "Review values and optionally patch the service contract?"
            ),
        })
    missing_required = sorted(
        key for key, item in audit.env.items()
        if item.required and item.value is None and not item.secret
    )
    if missing_required:
        uncertainties.append({
            "type": "missing_required_env_value",
            "service": service.name,
            "keys": missing_required,
            "question": (
                f"Required env values for {service.name} cannot be derived: "
                f"{', '.join(missing_required)}. What values should the reviewed plan use?"
            ),
        })
    missing_required_secrets = sorted(
        key for key, item in audit.env.items()
        if item.required and item.value is None and item.secret
    )
    if missing_required_secrets:
        uncertainties.append({
            "type": "missing_required_secret",
            "service": service.name,
            "keys": missing_required_secrets,
            "question": (
                f"Required secrets for {service.name} have no local source: "
                f"{', '.join(missing_required_secrets)}. Supply them in infra/.env, "
                "or explicitly add only locally-mintable keys to generated_secret_keys?"
            ),
        })
    if service.language == "node" and service.dockerfile_source == "generated":
        if not audit.start_script:
            uncertainties.append({
                "type": "missing_start_script",
                "service": service.name,
                "question": (
                    f"{service.name} has neither npm script 'prod' nor 'start:prod'; "
                    "which reviewed start script should the generated image run?"
                ),
            })
        if not audit.build_script:
            uncertainties.append({
                "type": "missing_build_script",
                "service": service.name,
                "question": (
                    f"{service.name} has no npm build script; which reviewed script should "
                    "the generated image run?"
                ),
            })
    if audit.prisma and service.dockerfile_source == "service":
        uncertainties.append({
            "type": "custom_prisma_migration_image",
            "service": service.name,
            "question": (
                f"{service.name} uses its own Dockerfile; confirm its runtime image contains "
                "the Prisma CLI and prisma/ schema for the generated one-shot migration, "
                "or set migration.mode=disabled?"
            ),
        })
    return uncertainties


def detection_report(root: Path) -> Dict[str, Any]:
    """Return machine-readable facts and suggested defaults without prompting."""
    cands = scan_candidates(root)
    safe_cands = [service for service in cands if _service_name_is_safe(service.name)]
    project_name = _normalize_compose_name(root.name)
    default_ports = _default_ports(safe_cands)
    modules = _default_modules(safe_cands)
    infra_ports = _default_infra_ports(
        modules,
        {host_port for host_port, _ in default_ports.values()},
    )
    services: Dict[str, Dict[str, Any]] = {}
    uncertainties: List[Dict[str, Any]] = []
    detected_port_owners: Dict[int, str] = {}

    if not cands:
        uncertainties.append({
            "type": "no_candidates",
            "question": (
                "No service candidates were found; is this the correct project root, "
                "or do services need to be added first?"
            ),
        })

    for service in cands:
        if not _service_name_is_safe(service.name):
            services[service.name] = {
                "include": False,
                "host_port": service.detected_port,
                "container_port": service.detected_port or 3000,
                "language": service.language,
                "has_dockerfile": service.has_dockerfile,
                "dockerfile": service.dockerfile_source,
                "detected_port": service.detected_port,
                "detected_infra": sorted(service.detected_infra),
            }
            uncertainties.append({
                "type": "invalid_service_name",
                "service": service.name,
                "question": (
                    f"Service folder {service.name!r} is not a safe Compose identifier; "
                    "rename it to letters/digits/dot/underscore/hyphen or keep it excluded?"
                ),
            })
            continue
        host_port, container_port = default_ports[service.name]
        services[service.name] = {
            "include": True,
            "host_port": host_port,
            "container_port": container_port,
            "language": service.language,
            "has_dockerfile": service.has_dockerfile,
            "dockerfile": service.dockerfile_source,
            "detected_port": service.detected_port,
            "detected_infra": sorted(service.detected_infra),
            **_default_service_contract(service),
        }
        uncertainties.extend(_contract_uncertainties(service))
        if service.detected_port is None:
            uncertainties.append({
                "type": "missing_port",
                "service": service.name,
                "question": (
                    f"No PORT was detected for {service.name}; "
                    f"use host {host_port} -> container {container_port}?"
                ),
            })
        elif service.detected_port in detected_port_owners:
            uncertainties.append({
                "type": "duplicate_port",
                "service": service.name,
                "conflicts_with": detected_port_owners[service.detected_port],
                "question": (
                    f"{service.name} and {detected_port_owners[service.detected_port]} "
                    f"both declare PORT={service.detected_port}; use host port {host_port} "
                    f"for {service.name}?"
                ),
            })
        else:
            detected_port_owners[service.detected_port] = service.name
            if host_port != service.detected_port:
                uncertainties.append({
                    "type": "host_port_unavailable",
                    "scope": "service",
                    "name": service.name,
                    "service": service.name,
                    "requested_port": service.detected_port,
                    "suggested_port": host_port,
                    "question": (
                        f"Host port {service.detected_port} for {service.name} is already "
                        f"reserved or listening; use {host_port} instead?"
                    ),
                })

        if not service.has_dockerfile and service.language != "node":
            uncertainties.append({
                "type": "missing_dockerfile",
                "service": service.name,
                "question": (
                    f"{service.name} is {service.language} and has no Dockerfile; "
                    + (
                        "use the generated Node Dockerfile or exclude it?"
                        if service.language == "node"
                        else "exclude it or add a Dockerfile before bootstrap?"
                    )
                ),
            })
        elif _dockerfile_has_fixed_identity(service.path / "Dockerfile"):
            if service.language == "node":
                dockerfile_question = (
                    "keep the reviewed service Dockerfile or use the generated Node fallback?"
                )
            else:
                dockerfile_question = (
                    "review/fix the service Dockerfile or exclude this service before bootstrap?"
                )
            uncertainties.append({
                "type": "dockerfile_fixed_identity",
                "service": service.name,
                "question": (
                    f"{service.name}/Dockerfile creates a user or group with a fixed numeric "
                    f"UID/GID, which can collide with the base image; {dockerfile_question}"
                ),
            })
        for dependency, module in AMBIGUOUS_DEP_HINTS.items():
            if dependency in service.deps:
                uncertainties.append({
                    "type": "ambiguous_module",
                    "service": service.name,
                    "dependency": dependency,
                    "module": module,
                    "question": (
                        f"{service.name} depends on {dependency}, which may use several "
                        f"transports; should {module} be enabled?"
                    ),
                })

    for image_name, colliding_services in _image_name_collisions(safe_cands).items():
        uncertainties.append({
            "type": "image_name_collision",
            "image": image_name,
            "services": colliding_services,
            "question": (
                f"Services {', '.join(colliding_services)} normalize to the same Docker image "
                f"name {image_name!r}; rename or exclude services until every image is unique?"
            ),
        })

    infra_service_names = _infra_service_names(modules)
    for service in safe_cands:
        if service.name.casefold() in infra_service_names:
            uncertainties.append({
                "type": "service_name_conflict",
                "service": service.name,
                "infra_modules": [
                    module
                    for module in modules
                    if service.name.casefold()
                    in {name.casefold() for name in INFRA_SERVICE_KEYS[module]}
                ],
                "question": (
                    f"App service {service.name!r} conflicts with an enabled infra service "
                    "in the merged Compose model; rename or exclude the app service, or "
                    "disable the conflicting infra module?"
                ),
            })

    for password_key, colliding_services in _db_secret_key_collisions(safe_cands).items():
        uncertainties.append({
            "type": "secret_key_collision",
            "secret_key": password_key,
            "services": colliding_services,
            "question": (
                f"Postgres services {', '.join(colliding_services)} normalize to the same "
                f"environment secret {password_key}; rename or exclude services until every "
                "database secret key is unique?"
            ),
        })

    for module in modules:
        for endpoint, (_, preferred_host_port) in INFRA_PORT_SPECS[module].items():
            suggested_port = infra_ports[module][endpoint]
            if suggested_port != preferred_host_port:
                uncertainties.append({
                    "type": "host_port_unavailable",
                    "scope": "infra",
                    "name": f"{module}.{endpoint}",
                    "module": module,
                    "endpoint": endpoint,
                    "requested_port": preferred_host_port,
                    "suggested_port": suggested_port,
                    "question": (
                        f"Host port {preferred_host_port} for {module}.{endpoint} is already "
                        f"reserved or listening; use {suggested_port} instead?"
                    ),
                })
    if cands and not modules:
        uncertainties.append({
            "type": "no_infra_detected",
            "question": "No infra modules were detected; should bootstrap run apps without shared infra?",
        })

    keycloak_spec, keycloak_uncertainties = _default_keycloak_spec(safe_cands)
    if "keycloak" in modules:
        uncertainties.extend(keycloak_uncertainties)
    kafka_spec = _default_kafka_spec(safe_cands)

    return {
        "project_root": str(root),
        "candidates": sorted(services),
        "suggested_config": {
            "project_name": project_name,
            "network_name": project_name,
            "services": services,
            "infra_modules": modules,
            "infra_ports": infra_ports,
            "keycloak": keycloak_spec,
            "kafka": kafka_spec,
        },
        "uncertainties": uncertainties,
    }


def load_plan_config(path: Path) -> Dict[str, Any]:
    try:
        config = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read init config {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(config, dict):
        print("error: init config must be a JSON object", file=sys.stderr)
        sys.exit(2)
    # Allow agents to save either suggested_config itself or the full detect report.
    suggested = config.get("suggested_config")
    if isinstance(suggested, dict):
        config = suggested
    return config


BASE_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9./:_@-]*$")
PG_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _config_error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(2)


def _apply_service_contract_config(
    service: ServiceCandidate,
    choice: Dict[str, Any],
) -> None:
    """Validate and apply the reviewed, per-service runtime contract."""
    audit = service.audit or ServiceAudit(name=service.name)
    raw_env = choice.get("env", {})
    if not isinstance(raw_env, dict):
        _config_error(f"config.services.{service.name}.env must be an object")
    env_values: Dict[str, Optional[str]] = {}
    for key, item in audit.env.items():
        if item.required or item.value is not None or "schema" in " ".join(item.sources):
            env_values[key] = item.value
    for key, value in raw_env.items():
        if not isinstance(key, str) or ENV_KEY_RE.fullmatch(key) is None:
            _config_error(
                f"config.services.{service.name}.env contains invalid key {key!r}"
            )
        if value is not None and not isinstance(value, (str, int, float, bool)):
            _config_error(
                f"config.services.{service.name}.env.{key} must be scalar or null"
            )
        env_values[key] = None if value is None else str(value)
    raw_secret_keys = choice.get(
        "secret_keys",
        [key for key, item in audit.env.items() if item.secret],
    )
    if not isinstance(raw_secret_keys, list) or not all(
        isinstance(key, str) and ENV_KEY_RE.fullmatch(key) for key in raw_secret_keys
    ):
        _config_error(f"config.services.{service.name}.secret_keys must be env key names")
    service.secret_keys = set(raw_secret_keys)
    raw_generated_secret_keys = choice.get("generated_secret_keys", [])
    if not isinstance(raw_generated_secret_keys, list) or not all(
        isinstance(key, str) and ENV_KEY_RE.fullmatch(key)
        for key in raw_generated_secret_keys
    ):
        _config_error(
            f"config.services.{service.name}.generated_secret_keys must be env key names"
        )
    service.generated_secret_keys = set(raw_generated_secret_keys)
    unknown_generated = service.generated_secret_keys.difference(service.secret_keys)
    if unknown_generated:
        _config_error(
            f"generated secrets for {service.name} must also appear in secret_keys: "
            f"{', '.join(sorted(unknown_generated))}"
        )
    missing = sorted(
        key
        for key, item in audit.env.items()
        if item.required
        and key not in service.secret_keys
        and (env_values.get(key) in {None, ""} or is_placeholder(env_values.get(key)))
    )
    if missing:
        _config_error(
            f"required env values for {service.name} need reviewed config: "
            f"{', '.join(missing)}"
        )
    service.env_values = env_values

    raw_refs = choice.get("service_refs", {})
    if not isinstance(raw_refs, dict):
        _config_error(f"config.services.{service.name}.service_refs must be an object")
    refs: Dict[str, str] = {}
    for key, target in raw_refs.items():
        if not isinstance(key, str) or ENV_KEY_RE.fullmatch(key) is None:
            _config_error(
                f"config.services.{service.name}.service_refs contains invalid key {key!r}"
            )
        if not isinstance(target, str) or not _service_name_is_safe(target):
            _config_error(
                f"config.services.{service.name}.service_refs.{key} must name a service"
            )
        refs[key] = target
    service.service_refs = refs

    health_path = choice.get("health_path", service.health_path)
    if health_path is False:
        health_path = None
    if health_path is not None and (
        not isinstance(health_path, str)
        or not health_path.startswith("/")
        or any(char.isspace() for char in health_path)
    ):
        _config_error(
            f"config.services.{service.name}.health_path must start with '/' or be null"
        )
    service.health_path = health_path

    start_script = choice.get("start_script", service.start_script)
    if start_script is not None and (
        not isinstance(start_script, str)
        or not re.fullmatch(r"[A-Za-z0-9:_-]+", start_script)
    ):
        _config_error(
            f"config.services.{service.name}.start_script must be an npm script name"
        )
    if service.dockerfile_source == "generated" and service.language == "node" and not start_script:
        _config_error(
            f"{service.name} needs a reviewed start_script because package.json has "
            "neither 'prod' nor 'start:prod'"
        )
    service.start_script = start_script

    build_script = choice.get("build_script", service.build_script)
    if build_script is not None and (
        not isinstance(build_script, str)
        or not re.fullmatch(r"[A-Za-z0-9:_-]+", build_script)
    ):
        _config_error(
            f"config.services.{service.name}.build_script must be an npm script name"
        )
    if service.dockerfile_source == "generated" and service.language == "node" and not build_script:
        _config_error(
            f"{service.name} needs a reviewed build_script because package.json has no 'build'"
        )
    service.build_script = build_script

    base_image = choice.get("base_image", service.base_image)
    if not isinstance(base_image, str) or BASE_IMAGE_RE.fullmatch(base_image) is None:
        _config_error(f"config.services.{service.name}.base_image is not a safe image reference")
    if service.dockerfile_source == "generated" and "slim" not in base_image:
        _config_error(
            f"generated Dockerfile for {service.name} requires a Debian slim base image"
        )
    service.base_image = base_image

    migration = choice.get("migration", {"mode": service.migration_mode})
    if not isinstance(migration, dict) or migration.get("mode", service.migration_mode) not in {
        "auto", "disabled"
    }:
        _config_error(
            f"config.services.{service.name}.migration.mode must be 'auto' or 'disabled'"
        )
    service.migration_mode = str(migration.get("mode", service.migration_mode))
    if service.migration_mode == "auto" and not audit.prisma:
        _config_error(f"auto migration for {service.name} requires a detected Prisma service")

    database = choice.get("database", {"schemas": service.db_schemas})
    if not isinstance(database, dict):
        _config_error(f"config.services.{service.name}.database must be an object")
    schemas = database.get("schemas", service.db_schemas)
    if not isinstance(schemas, list) or not all(
        isinstance(schema, str) and PG_SCHEMA_RE.fullmatch(schema) for schema in schemas
    ):
        _config_error(
            f"config.services.{service.name}.database.schemas must be safe schema names"
        )
    service.db_schemas = sorted(set(schemas))
    db_name = database.get("name", service.db_name or service.name)
    db_role = database.get("owner", service.db_role or service.name)
    password_env = database.get(
        "password_env",
        service.db_password_env or _service_db_password_key(service.name),
    )
    if not isinstance(db_name, str) or PG_SCHEMA_RE.fullmatch(db_name) is None:
        _config_error(f"config.services.{service.name}.database.name is invalid")
    if not isinstance(db_role, str) or PG_SCHEMA_RE.fullmatch(db_role) is None:
        _config_error(f"config.services.{service.name}.database.owner is invalid")
    if not isinstance(password_env, str) or ENV_KEY_RE.fullmatch(password_env) is None:
        _config_error(f"config.services.{service.name}.database.password_env is invalid")
    service.db_name = db_name
    service.db_role = db_role
    service.db_password_env = password_env


def _validate_keycloak_spec(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        _config_error("config.keycloak must be an object")
    mode = raw.get("mode", "generated-local")
    if mode not in {"generated-local", "official"}:
        _config_error("config.keycloak.mode must be 'generated-local' or 'official'")
    realms = raw.get("realms", [])
    if not isinstance(realms, list):
        _config_error("config.keycloak.realms must be a list")
    normalized: List[Dict[str, Any]] = []
    seen_realms: Set[str] = set()
    for realm in realms:
        if not isinstance(realm, dict):
            _config_error("each config.keycloak.realms item must be an object")
        name = realm.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            _config_error("each Keycloak realm needs a safe name")
        if name in seen_realms:
            _config_error(f"duplicate Keycloak realm {name!r}")
        seen_realms.add(name)
        roles = realm.get("roles", [])
        clients = realm.get("clients", [])
        if not isinstance(roles, list) or not all(isinstance(role, str) and role for role in roles):
            _config_error(f"Keycloak realm {name} roles must be non-empty strings")
        if not isinstance(clients, list):
            _config_error(f"Keycloak realm {name} clients must be a list")
        normalized_clients: List[Dict[str, Any]] = []
        for client in clients:
            if not isinstance(client, dict) or not isinstance(client.get("client_id"), str):
                _config_error(f"Keycloak realm {name} has an invalid client")
            kind = client.get("kind", "public")
            if kind not in {"public", "service-account"}:
                _config_error(f"Keycloak client {client['client_id']} has invalid kind")
            normalized_client = {
                "client_id": client["client_id"],
                "kind": kind,
            }
            client_roles = client.get("roles", [])
            if not isinstance(client_roles, list) or not all(
                isinstance(role, str) and role for role in client_roles
            ):
                _config_error(
                    f"Keycloak client {client['client_id']} roles must be non-empty strings"
                )
            normalized_client["roles"] = sorted(set(client_roles))
            if kind == "service-account":
                secret_env = client.get("secret_env")
                if not isinstance(secret_env, str) or ENV_KEY_RE.fullmatch(secret_env) is None:
                    _config_error(
                        f"service-account client {client['client_id']} needs secret_env"
                    )
                normalized_client["secret_env"] = secret_env
            normalized_clients.append(normalized_client)
        normalized.append({
            "name": name,
            "roles": sorted(set(roles)),
            "clients": normalized_clients,
        })
    seed_users = raw.get("seed_users", [])
    if not isinstance(seed_users, list):
        _config_error("config.keycloak.seed_users must be a list")
    normalized_users: List[Dict[str, Any]] = []
    for user in seed_users:
        if not isinstance(user, dict):
            _config_error("each Keycloak seed user must be an object")
        realm = user.get("realm")
        username = user.get("username")
        password_env = user.get("password_env")
        roles = user.get("realm_roles", [])
        if realm not in seen_realms or not isinstance(username, str) or not username:
            _config_error("each Keycloak seed user needs a known realm and username")
        if not isinstance(password_env, str) or ENV_KEY_RE.fullmatch(password_env) is None:
            _config_error(f"Keycloak seed user {username} needs password_env")
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            _config_error(f"Keycloak seed user {username} realm_roles must be strings")
        normalized_users.append({
            "realm": realm,
            "username": username,
            "password_env": password_env,
            "realm_roles": sorted(set(roles)),
        })
    return {"mode": mode, "realms": normalized, "seed_users": normalized_users}


def _validate_kafka_spec(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        _config_error("config.kafka must be an object")
    strict = raw.get("strict", True)
    topics = raw.get("topics", [])
    if not isinstance(strict, bool) or not isinstance(topics, list):
        _config_error("config.kafka requires boolean strict and list topics")
    normalized: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for topic in topics:
        if not isinstance(topic, dict) or not isinstance(topic.get("name"), str):
            _config_error("each config.kafka.topics item must be an object with name")
        name = topic["name"]
        if TOPIC_RE.fullmatch(name) is None:
            _config_error(f"Kafka topic {name!r} does not match the versioned topic contract")
        try:
            partitions = int(topic.get("partitions", 1))
            replicas = int(topic.get("replication_factor", 1))
        except (TypeError, ValueError):
            _config_error(f"Kafka topic {name!r} partition/replication values must be integers")
        if partitions < 1 or replicas != 1:
            _config_error(
                f"local single-broker topic {name!r} needs partitions>=1 and replication_factor=1"
            )
        topic_config = topic.get("config", {})
        if not isinstance(topic_config, dict) or not all(
            isinstance(key, str) and isinstance(value, (str, int, float, bool))
            for key, value in topic_config.items()
        ):
            _config_error(f"Kafka topic {name!r} config must contain scalar values")
        if name in seen:
            _config_error(f"duplicate Kafka topic {name!r}")
        seen.add(name)
        normalized.append({
            "name": name,
            "partitions": partitions,
            "replication_factor": replicas,
            "config": {key: str(value) for key, value in sorted(topic_config.items())},
        })
    return {"strict": strict, "topics": sorted(normalized, key=lambda item: item["name"])}


def build_plan(
    root: Path,
    force: bool,
    assume_defaults: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> InitPlan:
    print(f"scanning {root}...")
    cands = scan_candidates(root)
    if not cands:
        print("  no service candidates found (looked for package.json / pyproject.toml / requirements.txt / go.mod)")
        sys.exit(2)

    print(f"  detected {len(cands)} candidate(s):")
    for s in cands:
        infra = ",".join(sorted(s.detected_infra)) or "none"
        port_hint = f" port≈{s.detected_port}" if s.detected_port else ""
        dockerfile = " [Dockerfile✓]" if s.has_dockerfile else ""
        print(f"    - {s.name:35s} {s.language:6s}  infra=[{infra}]{port_hint}{dockerfile}")

    print()
    # 1) project meta
    default_name = _normalize_compose_name(root.name)
    if config is not None:
        project_name = config.get("project_name") or default_name
        network_name = config.get("network_name") or project_name
        for field_name, field_value, field_pattern, allowed in (
            (
                "project_name",
                project_name,
                COMPOSE_NAME_RE,
                "lowercase letters, digits, dashes, or underscores",
            ),
            (
                "network_name",
                network_name,
                COMPOSE_SERVICE_RE,
                "letters, digits, dots, dashes, or underscores",
            ),
        ):
            if not isinstance(field_value, str) or field_pattern.fullmatch(field_value) is None:
                print(
                    f"error: config.{field_name} must start with a letter/digit and contain "
                    f"only {allowed}",
                    file=sys.stderr,
                )
                sys.exit(2)
        print("  --config: using reviewed choices without prompting")
        print(f"  compose project name: {project_name}")
        print(f"  docker network name : {network_name}")
    elif assume_defaults:
        project_name = default_name
        network_name = project_name
        print("  --yes: using detected defaults without prompting")
        print(f"  compose project name: {project_name}")
        print(f"  docker network name : {network_name}")
    else:
        project_name = _normalize_compose_name(ask("Compose project name (docker -p)", default_name))
        network_name = _normalize_compose_name(ask("Docker network name", project_name))

    # 2) per-service confirm + port
    print("\n--- select services to include ---")
    next_port = 4000
    used_host_ports: Set[int] = set()
    default_ports = _default_ports(
        [service for service in cands if _service_name_is_safe(service.name)]
    )
    service_config = config.get("services", {}) if config is not None else {}
    if not isinstance(service_config, dict):
        print("error: config.services must be an object keyed by service name", file=sys.stderr)
        sys.exit(2)
    unknown_services = set(service_config).difference(service.name for service in cands)
    if unknown_services:
        print(
            f"error: config contains unknown services: {', '.join(sorted(unknown_services))}",
            file=sys.stderr,
        )
        sys.exit(2)
    for s in cands:
        choice = service_config.get(s.name, {})
        if not isinstance(choice, dict):
            print(f"error: config.services.{s.name} must be an object", file=sys.stderr)
            sys.exit(2)
        if config is not None:
            include = choice.get("include", True)
            if not isinstance(include, bool):
                print(f"error: config.services.{s.name}.include must be boolean", file=sys.stderr)
                sys.exit(2)
            s.include = include
        else:
            s.include = True if assume_defaults else ask_yn(f"include '{s.name}'?", True)
        if s.include:
            if not _service_name_is_safe(s.name):
                print(
                    f"error: service folder {s.name!r} is not a safe Compose identifier; "
                    "rename or exclude it",
                    file=sys.stderr,
                )
                sys.exit(2)
            default_dockerfile = "service" if s.has_dockerfile else "generated"
            if config is not None:
                dockerfile_source = choice.get("dockerfile", default_dockerfile)
            elif (
                not assume_defaults
                and s.has_dockerfile
                and _dockerfile_has_fixed_identity(s.path / "Dockerfile")
            ):
                keep_service = ask_yn(
                    f"  {s.name}/Dockerfile uses a fixed numeric UID/GID; keep it?",
                    True,
                )
                if keep_service:
                    dockerfile_source = "service"
                elif s.language == "node":
                    dockerfile_source = "generated"
                else:
                    s.include = False
                    print(
                        f"  exclude {s.name}: generated Dockerfiles are only supported "
                        "for Node services"
                    )
                    continue
            else:
                dockerfile_source = default_dockerfile
            if dockerfile_source not in {"service", "generated"}:
                print(
                    f"error: config.services.{s.name}.dockerfile must be "
                    "'service' or 'generated'",
                    file=sys.stderr,
                )
                sys.exit(2)
            if dockerfile_source == "service" and not s.has_dockerfile:
                print(
                    f"error: {s.name} selected its service Dockerfile, but none exists",
                    file=sys.stderr,
                )
                sys.exit(2)
            if dockerfile_source == "generated" and s.language != "node":
                if s.has_dockerfile:
                    message = (
                        f"{s.name} is {s.language}; generated Dockerfiles are only "
                        "supported for Node services, so select 'service' or exclude it"
                    )
                else:
                    message = (
                        f"{s.name} is {s.language} and has no Dockerfile; add one or "
                        "exclude the service via a reviewed --config plan"
                    )
                print(f"error: {message}", file=sys.stderr)
                sys.exit(2)
            s.dockerfile_source = str(dockerfile_source)
            if config is None and not assume_defaults:
                choice = dict(choice)
                if s.language == "node" and s.dockerfile_source == "generated" and not s.start_script:
                    choice["start_script"] = ask(
                        f"  npm start script for {s.name} (for example prod or start:prod)"
                    )
                if s.language == "node" and s.dockerfile_source == "generated" and not s.build_script:
                    choice["build_script"] = ask(
                        f"  npm build script for {s.name}"
                    )
                missing_values = [
                    key
                    for key, item in (s.audit.env if s.audit else {}).items()
                    if item.required and not item.secret and item.value in {None, ""}
                ]
                if missing_values:
                    reviewed_env = dict(choice.get("env", {}))
                    for key in missing_values:
                        reviewed_env[key] = ask(f"  value for required {s.name}.{key}")
                    choice["env"] = reviewed_env
            _apply_service_contract_config(s, choice)
            default_port = s.detected_port or next_port
            if config is not None or assume_defaults:
                suggested_host, suggested_container = default_ports[s.name]
                try:
                    s.host_port = int(choice.get("host_port", suggested_host))
                    s.container_port = int(choice.get("container_port", suggested_container))
                except (TypeError, ValueError):
                    print(f"error: ports for {s.name} must be integers", file=sys.stderr)
                    sys.exit(2)
                if not (1 <= s.host_port <= 65535 and 1 <= s.container_port <= 65535):
                    print(f"error: ports for {s.name} must be between 1 and 65535", file=sys.stderr)
                    sys.exit(2)
                if s.host_port in used_host_ports:
                    print(f"error: duplicate host port {s.host_port} in init config", file=sys.stderr)
                    sys.exit(2)
                if not _port_is_available(s.host_port):
                    print(
                        f"error: selected host port {s.host_port} for {s.name} is currently "
                        "in use; rerun --detect-json or free the listener",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                print(
                    f"  include {s.name}: "
                    f"host {s.host_port} -> container {s.container_port}"
                )
            else:
                s.host_port = ask_int(f"  host port for {s.name}", default_port)
                s.container_port = ask_int(f"  container port for {s.name}", s.detected_port or 3000)
            used_host_ports.add(s.host_port)
            next_port = s.host_port + 1

    included = [s for s in cands if s.include]
    if not included:
        print("no services selected. abort.")
        sys.exit(1)
    included_names = {service.name for service in included}
    for service in included:
        unknown_refs = set(service.service_refs.values()).difference(included_names)
        if unknown_refs:
            _config_error(
                f"service refs for {service.name} target excluded/unknown services: "
                f"{', '.join(sorted(unknown_refs))}"
            )
    image_collisions = _image_name_collisions(included)
    if image_collisions:
        details = "; ".join(
            f"{image}: {', '.join(names)}" for image, names in image_collisions.items()
        )
        print(
            f"error: services normalize to duplicate Docker image names ({details}); "
            "rename or exclude colliding services",
            file=sys.stderr,
        )
        sys.exit(2)
    # 3) aggregate detected infra + prompt each
    aggregate: Set[str] = set()
    for s in included:
        aggregate.update(s.detected_infra)
    print("\n--- infra modules ---")
    print(f"  auto-detected from tech stack: {sorted(aggregate) or 'none'}")
    if config is not None:
        configured_modules = config.get("infra_modules", _default_modules(included))
        if not isinstance(configured_modules, list) or not all(
            isinstance(module, str) for module in configured_modules
        ):
            print("error: config.infra_modules must be a list of module names", file=sys.stderr)
            sys.exit(2)
        unknown_modules = set(configured_modules).difference(INFRA_MODULES_ALL)
        if unknown_modules:
            print(
                f"error: unknown infra modules: {', '.join(sorted(unknown_modules))}",
                file=sys.stderr,
            )
            sys.exit(2)
        modules = [module for module in INFRA_MODULES_ALL if module in configured_modules]
        required_postgres = set(modules).intersection({"keycloak", "temporal"})
        if required_postgres and "postgres" not in modules:
            print(
                "error: postgres is required when keycloak or temporal is enabled",
                file=sys.stderr,
            )
            sys.exit(2)
        print(f"  --config: enabling {', '.join(modules) or '<none>'}")
    elif assume_defaults:
        # Keycloak and Temporal use the generated Postgres service internally.
        if aggregate.intersection({"keycloak", "temporal"}):
            aggregate.add("postgres")
        modules = [m for m in INFRA_MODULES_ALL if m in aggregate]
        print(f"  --yes: enabling {', '.join(modules) or '<none>'}")
    else:
        modules = []
        for m in INFRA_MODULES_ALL:
            default = m in aggregate
            if ask_yn(f"  enable '{m}'?", default):
                modules.append(m)

    infra_service_names = _infra_service_names(modules)
    service_name_conflicts = [
        service.name
        for service in included
        if service.name.casefold() in infra_service_names
    ]
    if service_name_conflicts:
        print(
            "error: app service names conflict with enabled infra services in the merged "
            f"Compose model: {', '.join(service_name_conflicts)}; rename/exclude the app "
            "services or disable the conflicting modules",
            file=sys.stderr,
        )
        sys.exit(2)
    if "postgres" in modules:
        secret_key_collisions = _db_secret_key_collisions(included)
        if secret_key_collisions:
            details = "; ".join(
                f"{secret_key}: {', '.join(names)}"
                for secret_key, names in secret_key_collisions.items()
            )
            print(
                f"error: Postgres services normalize to duplicate environment secret keys "
                f"({details}); rename/exclude colliding services or disable postgres",
                file=sys.stderr,
            )
            sys.exit(2)
        shared_conflicts = _shared_db_conflicts(included)
        if shared_conflicts:
            details = "; ".join(
                f"{database}: {', '.join(names)}"
                for database, names in shared_conflicts.items()
            )
            _config_error(
                "services sharing a Postgres database must use the same reviewed owner "
                f"and password_env ({details})"
            )

    # 4) host-side infra ports. Container ports stay fixed so app-to-infra URLs
    # remain stable; only published host ports move when occupied.
    print("\n--- infra host ports ---")
    suggested_infra_ports = _default_infra_ports(modules, used_host_ports)
    configured_infra_ports = config.get("infra_ports", {}) if config is not None else {}
    if not isinstance(configured_infra_ports, dict):
        print("error: config.infra_ports must be an object keyed by module", file=sys.stderr)
        sys.exit(2)
    unknown_port_modules = set(configured_infra_ports).difference(INFRA_MODULES_ALL)
    if unknown_port_modules:
        print(
            f"error: config.infra_ports contains unknown modules: "
            f"{', '.join(sorted(unknown_port_modules))}",
            file=sys.stderr,
        )
        sys.exit(2)

    infra_ports: Dict[str, Dict[str, int]] = {}
    for module in modules:
        raw_module_ports = configured_infra_ports.get(module, {})
        if not isinstance(raw_module_ports, dict):
            print(f"error: config.infra_ports.{module} must be an object", file=sys.stderr)
            sys.exit(2)
        unknown_endpoints = set(raw_module_ports).difference(INFRA_PORT_SPECS[module])
        if unknown_endpoints:
            print(
                f"error: config.infra_ports.{module} contains unknown endpoints: "
                f"{', '.join(sorted(unknown_endpoints))}",
                file=sys.stderr,
            )
            sys.exit(2)
        infra_ports[module] = {}
        for endpoint, (container_port, _) in INFRA_PORT_SPECS[module].items():
            suggested_host = suggested_infra_ports[module][endpoint]
            if config is not None or assume_defaults:
                raw_host = raw_module_ports.get(endpoint, suggested_host)
                try:
                    host_port = int(raw_host)
                except (TypeError, ValueError):
                    print(
                        f"error: config.infra_ports.{module}.{endpoint} must be an integer",
                        file=sys.stderr,
                    )
                    sys.exit(2)
            else:
                host_port = ask_int(
                    f"  host port for {module}.{endpoint} -> container {container_port}",
                    suggested_host,
                )
            if not 1 <= host_port <= 65535:
                print(
                    f"error: host port for {module}.{endpoint} must be between 1 and 65535",
                    file=sys.stderr,
                )
                sys.exit(2)
            if host_port in used_host_ports:
                print(
                    f"error: duplicate host port {host_port} across app/infra config",
                    file=sys.stderr,
                )
                sys.exit(2)
            if not _port_is_available(host_port):
                print(
                    f"error: selected host port {host_port} for {module}.{endpoint} is "
                    "currently in use; rerun --detect-json or free the listener",
                    file=sys.stderr,
                )
                sys.exit(2)
            infra_ports[module][endpoint] = host_port
            used_host_ports.add(host_port)
            print(f"  {module}.{endpoint}: host {host_port} -> container {container_port}")

    default_keycloak, keycloak_uncertainties = _default_keycloak_spec(included)
    raw_keycloak = config.get("keycloak", default_keycloak) if config is not None else default_keycloak
    if "keycloak" in modules and keycloak_uncertainties:
        unresolved = ", ".join(
            item.get("client") or item.get("role") or "unknown"
            for item in keycloak_uncertainties
        )
        if config is None or raw_keycloak == default_keycloak:
            _config_error(
                "Keycloak ownership is ambiguous for "
                f"{unresolved}; run --detect-json and provide a reviewed config.keycloak"
            )
    keycloak_spec = _validate_keycloak_spec(raw_keycloak)
    default_kafka = _default_kafka_spec(included)
    raw_kafka = config.get("kafka", default_kafka) if config is not None else default_kafka
    kafka_spec = _validate_kafka_spec(raw_kafka)

    return InitPlan(
        project_root=root,
        project_name=project_name,
        network_name=network_name,
        services=included,
        infra_modules=modules,
        infra_ports=infra_ports,
        keycloak_spec=keycloak_spec,
        kafka_spec=kafka_spec,
        force=force,
    )


# ---------- Compose YAML generation ----------

INFRA_SERVICE_YAML = {
    "postgres": """  postgres:
    image: postgres:16-alpine
    container_name: {proj}-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: '${{POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in infra/.env}}'
{postgres_service_env}{postgres_module_env}      POSTGRES_DB: postgres
    ports: ['{postgres_host_port}:5432']
    volumes:
      - pg-data:/var/lib/postgresql/data
      - ./pg-init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ['CMD-SHELL', 'pg_isready -U postgres']
      interval: 5s
      timeout: 3s
      retries: 20
    networks: [{net}]
""",
    "redis": """  redis:
    image: redis:7-alpine
    container_name: {proj}-redis
    restart: unless-stopped
    command: ['redis-server', '--appendonly', 'yes']
    ports: ['{redis_host_port}:6379']
    volumes:
      - redis-data:/data
    healthcheck:
      test: ['CMD', 'redis-cli', 'ping']
      interval: 5s
      timeout: 3s
      retries: 20
    networks: [{net}]
""",
    "kafka": """  redpanda:
    image: docker.redpanda.com/redpandadata/redpanda:v24.2.4
    container_name: {proj}-redpanda
    restart: unless-stopped
    command:
      - redpanda
      - start
      - --smp=1
      - --memory=512M
      - --overprovisioned
      - --node-id=0
      - --check=false
      - --kafka-addr=PLAINTEXT://0.0.0.0:29092,OUTSIDE://0.0.0.0:9092
      - --advertise-kafka-addr=PLAINTEXT://redpanda:29092,OUTSIDE://localhost:{kafka_host_port}
      - --pandaproxy-addr=0.0.0.0:8082
      - --advertise-pandaproxy-addr=localhost:{kafka_proxy_host_port}
    ports:
      - '{kafka_host_port}:9092'
      - '{kafka_proxy_host_port}:8082'
      - '{kafka_admin_host_port}:9644'
    volumes:
      - redpanda-data:/var/lib/redpanda/data
    healthcheck:
      test: ['CMD-SHELL', 'rpk cluster health | grep -q "Healthy:.*true"']
      interval: 10s
      timeout: 5s
      retries: 30
    networks: [{net}]
""",
    "keycloak": """  keycloak:
    image: quay.io/keycloak/keycloak:24.0
    container_name: {proj}-keycloak
    restart: unless-stopped
    command: [start-dev, --http-port=8080, --hostname-strict=false]
    environment:
      KEYCLOAK_ADMIN: admin
      KEYCLOAK_ADMIN_PASSWORD: '${{KEYCLOAK_ADMIN_PASSWORD:?set KEYCLOAK_ADMIN_PASSWORD in infra/.env}}'
      KC_DB: postgres
      KC_DB_URL: jdbc:postgresql://postgres:5432/keycloak
      KC_DB_USERNAME: keycloak
      KC_DB_PASSWORD: '${{KEYCLOAK_DB_PASSWORD:?set KEYCLOAK_DB_PASSWORD in infra/.env}}'
      KC_HEALTH_ENABLED: 'true'
      KC_HTTP_ENABLED: 'true'
      KC_HOSTNAME_STRICT: 'false'
    ports: ['{keycloak_host_port}:8080']
    depends_on: {{ postgres: {{ condition: service_healthy }} }}
    volumes:
      - keycloak-data:/opt/keycloak/data
    healthcheck:
      test: ['CMD-SHELL', 'exec 3<>/dev/tcp/localhost/8080 && printf "GET /health/ready HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n" >&3 && grep -q "200 OK" <&3']
      interval: 10s
      timeout: 5s
      retries: 30
      start_period: 60s
    networks: [{net}]
""",
    "temporal": """  temporal:
    image: temporalio/auto-setup:1.24.2
    container_name: {proj}-temporal
    restart: unless-stopped
    environment:
      DB: postgres12
      DB_PORT: '5432'
      POSTGRES_USER: temporal
      POSTGRES_PWD: '${{TEMPORAL_DB_PASSWORD:?set TEMPORAL_DB_PASSWORD in infra/.env}}'
      POSTGRES_SEEDS: postgres
      DBNAME: temporal
      VISIBILITY_DBNAME: temporal_visibility
      SKIP_DB_CREATE: 'true'
      DEFAULT_NAMESPACE: default
      DEFAULT_NAMESPACE_RETENTION: 24h
    ports: ['{temporal_host_port}:7233']
    depends_on: {{ postgres: {{ condition: service_healthy }} }}
    healthcheck:
      test: ['CMD-SHELL', 'temporal operator cluster health']
      interval: 15s
      timeout: 5s
      retries: 10
      start_period: 30s
    networks: [{net}]

  temporal-ui:
    image: temporalio/ui:2.30.0
    container_name: {proj}-temporal-ui
    restart: unless-stopped
    environment:
      TEMPORAL_ADDRESS: temporal:7233
      TEMPORAL_CORS_ORIGINS: http://localhost:3000
    ports: ['{temporal_ui_host_port}:8080']
    depends_on: [temporal]
    networks: [{net}]
""",
    "minio": """  minio:
    image: minio/minio:latest
    container_name: {proj}-minio
    restart: unless-stopped
    command: [server, /data, --console-address, ':9001']
    environment:
      MINIO_ROOT_USER: '${{MINIO_ROOT_USER:?set MINIO_ROOT_USER in infra/.env}}'
      MINIO_ROOT_PASSWORD: '${{MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD in infra/.env}}'
    ports: ['{minio_api_host_port}:9000', '{minio_console_host_port}:9001']
    volumes: [minio-data:/data]
    healthcheck:
      test: ['CMD', 'mc', 'ready', 'local']
      interval: 10s
      timeout: 3s
      retries: 20
    networks: [{net}]
""",
    "elasticsearch": """  elasticsearch:
    image: elasticsearch:8.17.0
    container_name: {proj}-elasticsearch
    restart: unless-stopped
    environment:
      discovery.type: single-node
      xpack.security.enabled: 'false'
      ES_JAVA_OPTS: '-Xms512m -Xmx512m'
    ports: ['{elasticsearch_host_port}:9200']
    volumes: [es-data:/usr/share/elasticsearch/data]
    healthcheck:
      test: ['CMD-SHELL', 'curl -fs http://localhost:9200/_cluster/health | grep -q "\\"status\\":\\"\\(green\\|yellow\\)\\""']
      interval: 10s
      timeout: 3s
      retries: 30
    networks: [{net}]
""",
}

MODULE_VOLUMES = {
    "postgres": ["pg-data"],
    "redis": ["redis-data"],
    "kafka": ["redpanda-data"],
    "keycloak": ["keycloak-data"],
    "minio": ["minio-data"],
    "elasticsearch": ["es-data"],
}

def render_node_dockerfile(service: ServiceCandidate) -> str:
    """Render a Debian-slim Node image from the audited service contract."""
    audit = service.audit or ServiceAudit(name=service.name)
    prisma_copy = "COPY prisma ./prisma\n" if audit.prisma else ""
    prisma_generate = "npx prisma generate && " if audit.prisma else ""
    runtime_prisma = "COPY --from=build /app/prisma ./prisma\n" if audit.prisma else ""
    healthcheck = ""
    if service.health_path:
        healthcheck = (
            "HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \\\n"
            f"  CMD curl -fsS http://localhost:{service.container_port}{service.health_path} || exit 1\n"
        )
    start_script = service.start_script or "prod"
    build_script = service.build_script or "build"
    return f"""# Auto-generated by infra-init from package.json + source contract.
# Local development image; pin a digest before reuse outside local dev.
FROM {service.base_image} AS deps
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
COPY package*.json ./
{prisma_copy}RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

FROM {service.base_image} AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN {prisma_generate}npm run {build_script}

FROM {service.base_image} AS runtime
WORKDIR /app
ENV NODE_ENV=production
RUN apt-get update && apt-get install -y --no-install-recommends \\
      openssl ca-certificates curl tini \\
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
{runtime_prisma}COPY package*.json ./
EXPOSE {service.container_port}
{healthcheck}ENTRYPOINT ["/usr/bin/tini", "--"]
USER node
CMD ["npm", "run", "{start_script}"]
"""


def _infra_template_values(plan: InitPlan) -> Dict[str, int]:
    values: Dict[str, int] = {}
    for module, endpoints in INFRA_PORT_SPECS.items():
        selected = plan.infra_ports.get(module, {})
        for endpoint, (_, preferred_host_port) in endpoints.items():
            values[f"{endpoint}_host_port"] = selected.get(endpoint, preferred_host_port)
    return values


def render_infra_yaml(plan: InitPlan) -> str:
    header = f"""# Auto-generated by infra-init on init.
# Shared infra for project '{plan.project_name}'.
# Bring up: cd infra && docker compose -f docker-compose.infra.yml up -d

name: {plan.project_name}

networks:
  {plan.network_name}:
    name: {plan.network_name}
    driver: bridge

volumes:
"""
    vols = []
    for m in plan.infra_modules:
        vols.extend(MODULE_VOLUMES.get(m, []))
    header += "".join(f"  {v}:\n" for v in vols) or "  {}\n"
    header += "\nservices:\n"
    postgres_service_env = "".join(
        f"      {password_env}: "
        f"'${{{password_env}:?set {password_env} in infra/.env}}'\n"
        for password_env in sorted({
            s.db_password_env or _service_db_password_key(s.name)
            for s in plan.services
            if "postgres" in s.detected_infra
        })
    )
    postgres_module_env = ""
    if "keycloak" in plan.infra_modules:
        postgres_module_env += (
            "      KEYCLOAK_DB_PASSWORD: "
            "'${KEYCLOAK_DB_PASSWORD:?set KEYCLOAK_DB_PASSWORD in infra/.env}'\n"
        )
    if "temporal" in plan.infra_modules:
        postgres_module_env += (
            "      TEMPORAL_DB_PASSWORD: "
            "'${TEMPORAL_DB_PASSWORD:?set TEMPORAL_DB_PASSWORD in infra/.env}'\n"
        )
    body = ""
    for m in plan.infra_modules:
        body += INFRA_SERVICE_YAML[m].format(
            proj=plan.project_name,
            net=plan.network_name,
            postgres_service_env=postgres_service_env,
            postgres_module_env=postgres_module_env,
            **_infra_template_values(plan),
        )
    if "postgres" in plan.infra_modules:
        body += f"""  postgres-provision:
    image: postgres:16-alpine
    profiles: [provision]
    restart: 'no'
    entrypoint: [bash, /provision/00-multi-db.sh]
    environment:
      PGHOST: postgres
      PGPORT: '5432'
      PGUSER: postgres
      PGPASSWORD: '${{POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in infra/.env}}'
{postgres_service_env}{postgres_module_env}    volumes:
      - ./pg-init:/provision:ro
    depends_on:
      postgres: {{ condition: service_healthy }}
    networks: [{plan.network_name}]

"""
    if (
        "keycloak" in plan.infra_modules
        and plan.keycloak_spec.get("mode") == "generated-local"
    ):
        secret_keys = sorted({
            client["secret_env"]
            for realm in plan.keycloak_spec.get("realms", [])
            for client in realm.get("clients", [])
            if client.get("kind") == "service-account" and client.get("secret_env")
        }.union({
            user["password_env"]
            for user in plan.keycloak_spec.get("seed_users", [])
            if user.get("password_env")
        }))
        client_env = "".join(
            f"      {key}: '${{{key}:?set {key} in infra/.env}}'\n"
            for key in secret_keys
        )
        body += f"""  keycloak-provision:
    image: python:3.12-alpine
    profiles: [provision]
    restart: 'no'
    command: [python, /provision/keycloak.py]
    environment:
      KEYCLOAK_ADMIN: admin
      KEYCLOAK_ADMIN_PASSWORD: '${{KEYCLOAK_ADMIN_PASSWORD:?set KEYCLOAK_ADMIN_PASSWORD in infra/.env}}'
      KEYCLOAK_BASE_URL: http://keycloak:8080
{client_env}    volumes:
      - ./provision:/provision:ro
      - ./contracts:/contracts:ro
    depends_on:
      keycloak: {{ condition: service_healthy }}
    networks: [{plan.network_name}]

"""
    if "kafka" in plan.infra_modules:
        body += f"""  kafka-provision:
    image: docker.redpanda.com/redpandadata/redpanda:v24.2.4
    profiles: [provision]
    restart: 'no'
    entrypoint: [/bin/bash, /provision/kafka.sh]
    volumes:
      - ./provision:/provision:ro
      - ./contracts:/contracts:ro
    depends_on:
      redpanda: {{ condition: service_healthy }}
    networks: [{plan.network_name}]

"""
    return header + body


def render_apps_yaml(plan: InitPlan) -> str:
    header = f"""# Auto-generated by infra-init.
# App services for project '{plan.project_name}'.
# Bring up (after infra healthy):
#   docker compose -f docker-compose.infra.yml -f docker-compose.apps.yml up -d --build

name: {plan.project_name}

services:
"""
    body = ""
    for s in plan.services:
        df_path = (
            f"../infra/dockerfiles/Dockerfile.{s.name}"
            if s.dockerfile_source == "generated"
            else "Dockerfile"
        )
        env_lines = _env_for_service(s, plan)
        env_block = "".join(f"      {k}: {_yaml_quote(v)}\n" for k, v in env_lines)
        depends_on = _depends_on(plan)
        image = (
            f"{_normalize_image_component(plan.project_name, 'project')}/"
            f"{_normalize_image_component(s.name)}:local"
        )
        if s.migration_mode == "auto":
            migration_depends = _depends_on(plan)
            body += f"""  {s.name}-migrate:
    image: {image}
    profiles: [migrate]
    restart: 'no'
    command: [npx, prisma, migrate, deploy]
    environment:
      NODE_ENV: staging
      PORT: '{s.container_port}'
{env_block}    depends_on:
{migration_depends}
    networks: [{plan.network_name}]

"""
        body += f"""  {s.name}:
    build:
      context: ../{s.name}
      dockerfile: {df_path}
    image: {image}
    restart: unless-stopped
    environment:
      NODE_ENV: staging
      PORT: '{s.container_port}'
{env_block}    ports:
      - '{s.host_port}:{s.container_port}'
    depends_on:
{depends_on}
    networks: [{plan.network_name}]

"""
    return header + body


def _yaml_quote(v: str) -> str:
    if v.startswith("${") or any(c in v for c in ":#'\"@"):
        return f"'{v}'"
    return v


def _service_alias(service_name: str) -> str:
    alias = re.sub(r"[^A-Za-z0-9]+", "_", service_name).strip("_").upper()
    return alias[2:] if alias.startswith("D_") else alias


def _infer_service_ref(
    key: str,
    source: ServiceCandidate,
    services: List[ServiceCandidate],
) -> Optional[ServiceCandidate]:
    explicit = source.service_refs.get(key)
    if explicit:
        return next((item for item in services if item.name == explicit), None)
    matches = [
        item
        for item in services
        if item.name != source.name and _service_alias(item.name) in key
    ]
    return matches[0] if len(matches) == 1 else None


def _rewrite_http_endpoint(value: str, host: str, port: int) -> str:
    match = re.match(r"^(https?://)(?:[^/@]+@)?[^/:?#]+(?::\d+)?(.*)$", value)
    if match:
        return f"{match.group(1)}{host}:{port}{match.group(2)}"
    return f"http://{host}:{port}"


def _rewrite_contract_value(
    service: ServiceCandidate,
    plan: InitPlan,
    key: str,
    value: str,
) -> str:
    upper = key.upper()
    modules = set(plan.infra_modules)
    if upper == "DATABASE_URL" and "postgres" in modules:
        password_key = service.db_password_env or _service_db_password_key(service.name)
        encoded_key = _urlencoded_db_password_key(password_key)
        password_ref = (
            f"${{{encoded_key}:?run infra-up to derive {encoded_key} from {password_key}}}"
        )
        role = service.db_role or service.name
        database = service.db_name or service.name
        userinfo = f"{role}:{password_ref}"
        return "postgresql://" + userinfo + f"@postgres:5432/{database}"
    if upper == "REDIS_HOST" and "redis" in modules:
        return "redis"
    if upper == "REDIS_PORT" and "redis" in modules:
        return "6379"
    if "REDIS" in upper and ("URL" in upper or value.startswith(("redis://", "rediss://"))) and "redis" in modules:
        scheme = "rediss" if value.startswith("rediss://") else "redis"
        return f"{scheme}://redis:6379"
    if upper in {"KAFKA_BROKERS", "KAFKA_BOOTSTRAP_SERVERS"} and "kafka" in modules:
        return "redpanda:29092"
    if upper == "KAFKA_HOST" and "kafka" in modules:
        return "redpanda"
    if upper == "KAFKA_PORT" and "kafka" in modules:
        return "29092"
    if upper.startswith("KEYCLOAK_") and (
        value.startswith(("http://", "https://")) or upper.endswith(("URL", "URI"))
    ) and "keycloak" in modules:
        return _rewrite_http_endpoint(value, "keycloak", 8080)
    if upper in {"TEMPORAL_ADDRESS", "TEMPORAL_HOST_PORT"} and "temporal" in modules:
        return "temporal:7233"
    if upper == "TEMPORAL_HOST" and "temporal" in modules:
        return "temporal"
    if upper == "TEMPORAL_PORT" and "temporal" in modules:
        return "7233"
    if upper in {"S3_ENDPOINT", "S3_ENDPOINT_URL", "MINIO_ENDPOINT"} and "minio" in modules:
        return _rewrite_http_endpoint(value, "minio", 9000)
    if "ELASTIC" in upper and "URL" in upper and "elasticsearch" in modules:
        return _rewrite_http_endpoint(value, "elasticsearch", 9200)
    target = _infer_service_ref(key, service, plan.services)
    if target and (value.startswith(("http://", "https://")) or upper.endswith(("URL", "URI"))):
        return _rewrite_http_endpoint(value, target.name, target.container_port)
    return value


def _env_for_service(s: ServiceCandidate, plan: InitPlan) -> List[Tuple[str, str]]:
    audit = s.audit or ServiceAudit(name=s.name)
    env: Dict[str, str] = {}
    values = dict(s.env_values)
    if audit.prisma and "postgres" in plan.infra_modules:
        values.setdefault("DATABASE_URL", "")
    for key, raw in sorted(values.items()):
        if key in {"NODE_ENV", "PORT"}:
            continue
        evidence = audit.env.get(key)
        if key in s.secret_keys or (evidence.secret if evidence else is_secret_key(key)):
            env[key] = f"${{{key}:?set {key} in infra/.env}}"
            continue
        if raw is None:
            continue
        env[key] = _rewrite_contract_value(s, plan, key, raw)
    return sorted(env.items())


def _depends_on(
    plan: InitPlan,
    migration_service: Optional[ServiceCandidate] = None,
) -> str:
    conds = []
    for m in plan.infra_modules:
        host = MODULE_HOST[m]
        if m == "temporal":
            conds.append(f"      {host}:\n        condition: service_started")
        elif m in ("postgres", "redis", "kafka", "keycloak", "minio", "elasticsearch"):
            conds.append(f"      {host}:\n        condition: service_healthy")
    if migration_service is not None:
        conds.append(
            f"      {migration_service.name}-migrate:\n"
            "        condition: service_completed_successfully"
        )
    return "\n".join(conds) if conds else "      {}"


def render_env_example(plan: InitPlan) -> str:
    lines = [
        "# Local-development secrets. Copy to `.env`; never commit that file.",
        "# GENERATE_ME values are local-owned and bootstrap mints them safely.",
        "# REPLACE_ME values are external credentials and must be supplied explicitly.",
        "",
    ]
    secrets: Dict[str, bool] = {}

    def add_secret(key: str, generated: bool) -> None:
        # A key shared with a known local provisioner is safe to mint even if
        # the app contract also references it.
        secrets[key] = secrets.get(key, False) or generated

    if "postgres" in plan.infra_modules:
        add_secret("POSTGRES_PASSWORD", True)
        for service in plan.services:
            if "postgres" in service.detected_infra:
                add_secret(
                    service.db_password_env or _service_db_password_key(service.name), True
                )
    if "keycloak" in plan.infra_modules:
        add_secret("KEYCLOAK_ADMIN_PASSWORD", True)
        add_secret("KEYCLOAK_DB_PASSWORD", True)
        for realm in plan.keycloak_spec.get("realms", []):
            for client in realm.get("clients", []):
                if client.get("secret_env"):
                    add_secret(
                        client["secret_env"],
                        plan.keycloak_spec.get("mode") == "generated-local",
                    )
        for user in plan.keycloak_spec.get("seed_users", []):
            if user.get("password_env"):
                add_secret(
                    user["password_env"],
                    plan.keycloak_spec.get("mode") == "generated-local",
                )
    if "temporal" in plan.infra_modules:
        add_secret("TEMPORAL_DB_PASSWORD", True)
    if "minio" in plan.infra_modules:
        add_secret("MINIO_ROOT_USER", True)
        add_secret("MINIO_ROOT_PASSWORD", True)
    for service in plan.services:
        audit = service.audit or ServiceAudit(name=service.name)
        for key in service.env_values:
            evidence = audit.env.get(key)
            if key in service.secret_keys or (evidence.secret if evidence else is_secret_key(key)):
                add_secret(key, key in service.generated_secret_keys)
    for key, generated in secrets.items():
        marker = "GENERATE_ME" if generated else "REPLACE_ME"
        lines.append(f"{key}={marker}_{key}")
    lines.append("")
    return "\n".join(lines)


def _service_db_password_key(service_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", service_name).strip("_").upper()
    return f"{normalized}_DB_PASSWORD"


def _urlencoded_db_password_key(password_key: str) -> str:
    return f"{password_key}_URLENCODED"


def _db_secret_key_collisions(services: List[ServiceCandidate]) -> Dict[str, List[str]]:
    owners: Dict[str, List[ServiceCandidate]] = {}
    for service in services:
        if "postgres" in service.detected_infra:
            key = service.db_password_env or _service_db_password_key(service.name)
            owners.setdefault(key, []).append(service)
    return {
        secret_key: [service.name for service in candidates]
        for secret_key, candidates in owners.items()
        if len({(service.db_role, service.db_name) for service in candidates}) > 1
    }


def _shared_db_conflicts(services: List[ServiceCandidate]) -> Dict[str, List[str]]:
    databases: Dict[str, List[ServiceCandidate]] = {}
    for service in services:
        if "postgres" in service.detected_infra:
            databases.setdefault(service.db_name or service.name, []).append(service)
    return {
        database: [service.name for service in candidates]
        for database, candidates in databases.items()
        if len({(service.db_role, service.db_password_env) for service in candidates}) > 1
    }


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def render_pg_init(plan: InitPlan) -> str:
    body = r"""#!/usr/bin/env bash
# Auto-generated local-dev reconciler. Safe on both a fresh and an existing volume.
set -euo pipefail

ensure_role_and_db() {
  local role="$1" password_var="$2" database="$3"
  local password="${!password_var:?set $password_var}"
  ROLE_PASSWORD="$password" psql -v ON_ERROR_STOP=1 --dbname postgres \
    --set=role_name="$role" <<'EOSQL'
\getenv role_password ROLE_PASSWORD
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role_name', :'role_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role_name') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'role_name', :'role_password') \gexec
EOSQL
  if ! psql -v ON_ERROR_STOP=1 --dbname postgres --tuples-only --no-align \
      --set=db_name="$database" \
      --command="SELECT 1 FROM pg_database WHERE datname = :'db_name'" | grep -qx 1; then
    createdb --owner="$role" "$database"
  fi
  psql -v ON_ERROR_STOP=1 --dbname postgres \
    --set=db_name="$database" --set=role_name="$role" <<'EOSQL'
SELECT format('ALTER DATABASE %I OWNER TO %I', :'db_name', :'role_name') \gexec
EOSQL
}

ensure_schema() {
  local database="$1" schema="$2" role="$3"
  psql -v ON_ERROR_STOP=1 --dbname "$database" \
    --set=schema_name="$schema" --set=role_name="$role" <<'EOSQL'
SELECT format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION %I', :'schema_name', :'role_name') \gexec
SELECT format('ALTER SCHEMA %I OWNER TO %I', :'schema_name', :'role_name') \gexec
EOSQL
}

"""
    for service in plan.services:
        if "postgres" not in service.detected_infra:
            continue
        database = service.db_name or service.name
        role = service.db_role or service.name
        password_key = service.db_password_env or _service_db_password_key(service.name)
        body += (
            f'ensure_role_and_db {shlex.quote(role)} {password_key} '
            f'{shlex.quote(database)}\n'
        )
        for schema in service.db_schemas:
            body += (
                f'ensure_schema {shlex.quote(database)} {shlex.quote(schema)} '
                f'{shlex.quote(role)}\n'
            )
        body += "\n"
    if "keycloak" in plan.infra_modules:
        body += "ensure_role_and_db keycloak KEYCLOAK_DB_PASSWORD keycloak\n"
    if "temporal" in plan.infra_modules:
        body += "ensure_role_and_db temporal TEMPORAL_DB_PASSWORD temporal\n"
        body += "ensure_role_and_db temporal TEMPORAL_DB_PASSWORD temporal_visibility\n"
    return body


def postgres_contract(plan: InitPlan) -> Dict[str, Any]:
    databases: List[Dict[str, Any]] = []
    for service in plan.services:
        if "postgres" in service.detected_infra:
            databases.append({
                "name": service.db_name or service.name,
                "owner": service.db_role or service.name,
                "password_env": service.db_password_env or _service_db_password_key(service.name),
                "schemas": list(service.db_schemas),
            })
    if "keycloak" in plan.infra_modules:
        databases.append({
            "name": "keycloak", "owner": "keycloak",
            "password_env": "KEYCLOAK_DB_PASSWORD", "schemas": [],
        })
    if "temporal" in plan.infra_modules:
        for database in ("temporal", "temporal_visibility"):
            databases.append({
                "name": database, "owner": "temporal",
                "password_env": "TEMPORAL_DB_PASSWORD", "schemas": [],
            })
    return {"databases": databases}


def env_contract(plan: InitPlan) -> Dict[str, Any]:
    services: Dict[str, Any] = {}
    for service in plan.services:
        audit = service.audit or ServiceAudit(name=service.name)
        rendered = dict(_env_for_service(service, plan))
        rendered.update({"NODE_ENV": "staging", "PORT": str(service.container_port)})
        secret_checks: List[Dict[str, str]] = []
        for key, value in rendered.items():
            references = re.findall(r"\$\{([A-Z][A-Z0-9_]*):\?", value)
            for secret_env in references:
                secret_checks.append({
                    "secret_env": secret_env,
                    "container_key": key,
                    "mode": "url_password" if key == "DATABASE_URL" else "direct",
                })
        services[service.name] = {
            "required_keys": sorted(rendered),
            "secret_keys": sorted(
                key
                for key in rendered
                if key in service.secret_keys
                or (audit.env.get(key).secret if audit.env.get(key) else is_secret_key(key))
            ),
            "expected": {
                key: item.expected
                for key, item in audit.env.items()
                if item.expected and key in rendered
            },
            "health_path": service.health_path,
            "secret_checks": secret_checks,
            "host_port": service.host_port,
            "container_port": service.container_port,
            "contract_sources": {
                key: sorted(item.sources) for key, item in sorted(audit.env.items())
            },
        }
    return {
        "services": services,
        "platform_allowlist": [
            "HOME", "HOSTNAME", "PATH", "PWD", "SHLVL",
            "NPM_CONFIG_*", "NODE_VERSION", "YARN_VERSION",
        ],
    }


def render_keycloak_provisioner() -> str:
    return r'''#!/usr/bin/env python3
"""Idempotently reconcile generated local-dev Keycloak realms."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("KEYCLOAK_BASE_URL", "http://keycloak:8080").rstrip("/")
CONTRACT_PATH = os.environ.get("KEYCLOAK_CONTRACT_PATH", "/contracts/keycloak.json")
with open(CONTRACT_PATH, encoding="utf-8") as contract_file:
    CONTRACT = json.load(contract_file)


def raw_request(path, method="GET", payload=None, form=None, token=None, allow=()):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        if exc.code in allow:
            return exc.code, None
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Keycloak {method} {path} failed HTTP {exc.code}: {body[:500]}")


_, token_payload = raw_request(
    "/realms/master/protocol/openid-connect/token",
    method="POST",
    form={
        "client_id": "admin-cli",
        "username": os.environ["KEYCLOAK_ADMIN"],
        "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"],
        "grant_type": "password",
    },
)
TOKEN = token_payload["access_token"]


def request(path, method="GET", payload=None, allow=()):
    return raw_request(path, method=method, payload=payload, token=TOKEN, allow=allow)


def ensure_realm(name):
    quoted = urllib.parse.quote(name, safe="")
    status, _ = request(f"/admin/realms/{quoted}", allow=(404,))
    if status == 404:
        request("/admin/realms", method="POST", payload={"realm": name, "enabled": True})
        print(f"created realm {name}")
    else:
        request(f"/admin/realms/{quoted}", method="PUT", payload={"realm": name, "enabled": True})
        print(f"reconciled realm {name}")


def ensure_realm_role(realm, role):
    rp = urllib.parse.quote(realm, safe="")
    rolep = urllib.parse.quote(role, safe="")
    status, _ = request(f"/admin/realms/{rp}/roles/{rolep}", allow=(404,))
    if status == 404:
        request(f"/admin/realms/{rp}/roles", method="POST", payload={"name": role})


def find_client(realm, client_id):
    rp = urllib.parse.quote(realm, safe="")
    query = urllib.parse.urlencode({"clientId": client_id})
    _, clients = request(f"/admin/realms/{rp}/clients?{query}")
    return clients[0] if clients else None


def ensure_client(realm, spec):
    rp = urllib.parse.quote(realm, safe="")
    client_id = spec["client_id"]
    service_account = spec["kind"] == "service-account"
    representation = {
        "clientId": client_id,
        "enabled": True,
        "publicClient": not service_account,
        "standardFlowEnabled": not service_account,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": service_account,
    }
    if not service_account:
        representation.update({"redirectUris": ["*"], "webOrigins": ["*"]})
    else:
        representation["secret"] = os.environ[spec["secret_env"]]
    current = find_client(realm, client_id)
    if current is None:
        request(f"/admin/realms/{rp}/clients", method="POST", payload=representation)
        current = find_client(realm, client_id)
    else:
        representation["id"] = current["id"]
        request(
            f"/admin/realms/{rp}/clients/{current['id']}",
            method="PUT",
            payload=representation,
        )
    for role in spec.get("roles", []):
        rolep = urllib.parse.quote(role, safe="")
        status, _ = request(
            f"/admin/realms/{rp}/clients/{current['id']}/roles/{rolep}",
            allow=(404,),
        )
        if status == 404:
            request(
                f"/admin/realms/{rp}/clients/{current['id']}/roles",
                method="POST",
                payload={"name": role},
            )
    if service_account:
        grant_local_admin(realm, current["id"])
        _, result = raw_request(
            f"/realms/{rp}/protocol/openid-connect/token",
            method="POST",
            form={
                "client_id": client_id,
                "client_secret": os.environ[spec["secret_env"]],
                "grant_type": "client_credentials",
            },
        )
        if not result or "access_token" not in result:
            raise RuntimeError(f"client_credentials smoke check failed for {realm}/{client_id}")
    print(f"reconciled client {realm}/{client_id}")


def grant_local_admin(realm, client_uuid):
    rp = urllib.parse.quote(realm, safe="")
    _, user = request(f"/admin/realms/{rp}/clients/{client_uuid}/service-account-user")
    _, management = request(
        f"/admin/realms/{rp}/clients?"
        + urllib.parse.urlencode({"clientId": "realm-management"})
    )
    management_uuid = management[0]["id"]
    _, role = request(
        f"/admin/realms/{rp}/clients/{management_uuid}/roles/realm-admin"
    )
    request(
        f"/admin/realms/{rp}/users/{user['id']}/role-mappings/clients/{management_uuid}",
        method="POST",
        payload=[role],
    )


def ensure_user(spec):
    realm = spec["realm"]
    rp = urllib.parse.quote(realm, safe="")
    query = urllib.parse.urlencode({"username": spec["username"], "exact": "true"})
    _, users = request(f"/admin/realms/{rp}/users?{query}")
    if users:
        user_id = users[0]["id"]
    else:
        request(
            f"/admin/realms/{rp}/users",
            method="POST",
            payload={"username": spec["username"], "enabled": True},
        )
        _, users = request(f"/admin/realms/{rp}/users?{query}")
        user_id = users[0]["id"]
    request(
        f"/admin/realms/{rp}/users/{user_id}/reset-password",
        method="PUT",
        payload={
            "type": "password",
            "temporary": False,
            "value": os.environ[spec["password_env"]],
        },
    )
    roles = []
    for role_name in spec.get("realm_roles", []):
        _, role = request(
            f"/admin/realms/{rp}/roles/{urllib.parse.quote(role_name, safe='')}"
        )
        roles.append(role)
    if roles:
        request(
            f"/admin/realms/{rp}/users/{user_id}/role-mappings/realm",
            method="POST",
            payload=roles,
        )
    print(f"reconciled seed user {realm}/{spec['username']}")


if CONTRACT.get("mode") != "generated-local":
    print("official Keycloak contract selected; generated reconciliation skipped")
    sys.exit(0)
for realm in CONTRACT.get("realms", []):
    ensure_realm(realm["name"])
    for role in realm.get("roles", []):
        ensure_realm_role(realm["name"], role)
    for client in realm.get("clients", []):
        ensure_client(realm["name"], client)
for user in CONTRACT.get("seed_users", []):
    ensure_user(user)
print("Keycloak local-dev contract verified")
'''


def render_kafka_provisioner(plan: InitPlan) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "# Auto-generated local-dev Kafka topic reconciler.",
        "set -euo pipefail",
        "BROKERS=redpanda:29092",
        "",
        "topic_shape() {",
        "  awk '$1 == \"NAME\" && $2 == \"PARTITIONS\" && $3 == \"REPLICAS\" { getline; print $2, $3; exit }'",
        "}",
        "",
        "reconcile_topic() {",
        "  local name=\"$1\" wanted_partitions=\"$2\" wanted_replicas=\"$3\"",
        "  local description current_partitions current_replicas delta",
        "  if ! description=\"$(rpk -X brokers=$BROKERS topic describe \"$name\" 2>/dev/null)\"; then",
        "    rpk -X brokers=$BROKERS topic create \"$name\" --partitions \"$wanted_partitions\" --replicas \"$wanted_replicas\"",
        "    description=\"$(rpk -X brokers=$BROKERS topic describe \"$name\")\"",
        "  fi",
        "  read -r current_partitions current_replicas < <(printf '%s\\n' \"$description\" | topic_shape)",
        "  [[ \"$current_partitions\" =~ ^[0-9]+$ && \"$current_replicas\" =~ ^[0-9]+$ ]] || {",
        "    echo \"error: cannot parse topic shape for $name\" >&2; exit 1;",
        "  }",
        "  if (( current_partitions < wanted_partitions )); then",
        "    delta=$((wanted_partitions - current_partitions))",
        "    rpk -X brokers=$BROKERS topic add-partitions \"$name\" --num \"$delta\"",
        "    current_partitions=$wanted_partitions",
        "  fi",
        "  if (( current_partitions != wanted_partitions )); then",
        "    echo \"error: topic $name has $current_partitions partitions; reviewed contract requires $wanted_partitions (partition count cannot be reduced safely)\" >&2",
        "    exit 1",
        "  fi",
        "  if (( current_replicas != wanted_replicas )); then",
        "    echo \"error: topic $name has replication $current_replicas; reviewed local single-broker contract requires $wanted_replicas\" >&2",
        "    exit 1",
        "  fi",
        "}",
    ]
    for topic in plan.kafka_spec.get("topics", []):
        name = shlex.quote(topic["name"])
        lines.append(
            f"reconcile_topic {name} {topic['partitions']} {topic['replication_factor']}"
        )
        for key, value in sorted(topic.get("config", {}).items()):
            setting = shlex.quote(f"{key}={value}")
            lines.append(
                f"rpk -X brokers=$BROKERS topic alter-config {name} --set {setting}"
            )
        lines.append(f"rpk -X brokers=$BROKERS topic describe {name} >/dev/null")
    if plan.kafka_spec.get("strict"):
        lines.append(
            "rpk -X brokers=$BROKERS cluster config set auto_create_topics_enabled false"
        )
    else:
        lines.append(
            "echo 'Kafka auto-create remains enabled: no complete reviewed topic contract.'"
        )
    lines.append("echo 'Kafka topic contract verified'")
    return "\n".join(lines) + "\n"


def render_readme(plan: InitPlan) -> str:
    svc_rows = "\n".join(
        f"| {s.name} | {s.host_port} | {s.container_port} |" for s in plan.services
    )
    infra_rows = "\n".join(
        f"| {module}.{endpoint} | {host_port} | {container_port} |"
        for module in plan.infra_modules
        for endpoint, (container_port, _) in INFRA_PORT_SPECS[module].items()
        for host_port in [plan.infra_ports.get(module, {}).get(
            endpoint,
            INFRA_PORT_SPECS[module][endpoint][1],
        )]
    )
    return f"""# {plan.project_name} — Shared Infra

Auto-scaffolded by `scripts/infra-init.py`.

## Bring up

```bash
../scripts/bootstrap.sh --yes
```

`infra-up.sh` starts base infra, reconciles live Postgres schemas, Keycloak
realms/clients/roles and Kafka topics, runs Prisma migrations once, then starts apps.
Bootstrap mints only `GENERATE_ME_*` local secrets and stops on any external
`REPLACE_ME_*` credential that still needs an explicit value.
The generated Keycloak permissions and wildcard redirect URIs are **local-dev only**.
Select `keycloak.mode=official` in a reviewed init config when Security owns realms.

Or use wrappers:
- `../scripts/infra-up.sh` — provision + migrate + up all
- `../scripts/infra-up.sh --infra-only` — provision shared infra only
- `../scripts/docker-apps-up.sh [service...]` — apps only, infra assumed healthy
- `../scripts/infra-down.sh [--volumes]` — down; add `--volumes` to wipe data

## App port map

| Service | Host | Container |
|---------|------|-----------|
{svc_rows}

## Infra services

| Endpoint | Host | Container |
|----------|------|-----------|
{infra_rows}

## Verifying env

```bash
../scripts/sync-env-docker.py verify <service>       # if wired into scripts/
# or from anywhere via skill: /sync-env-docker
```

The verifier uses `infra/contracts/env.json`, which is the audited union of
`.env.example`, source configuration calls, validation schema and Prisma env usage.
"""


# ---------- Write ----------

def _merge_preserved_env(previous: Path, example: Path, target: Path) -> None:
    """Preserve existing credentials and append newly generated contract keys."""
    old_lines = previous.read_text().splitlines()
    known = {
        match.group(1)
        for line in old_lines
        for match in [re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)]
        if match
    }
    additions = []
    for line in example.read_text().splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) not in known:
            additions.append(line)
            known.add(match.group(1))
    merged = list(old_lines)
    if additions:
        if merged and merged[-1]:
            merged.append("")
        merged.append("# Added by regenerated docker-claude contract; review REPLACE_ME values.")
        merged.extend(additions)
    target.write_text("\n".join(merged) + "\n")
    target.chmod(0o600)


def write_plan(plan: InitPlan) -> None:
    infra_dir = plan.project_root / "infra"
    preserved_env: Optional[Path] = None
    if infra_dir.exists() and not plan.force:
        print(f"\nerror: {infra_dir} already exists. Use --force to overwrite.")
        sys.exit(1)
    if infra_dir.exists() and plan.force:
        backup = plan.project_root / f"infra.backup.{_ts()}"
        print(f"backing up existing infra/ → {backup.name}/")
        shutil.move(str(infra_dir), str(backup))
        candidate = backup / ".env"
        if candidate.is_file():
            preserved_env = candidate

    infra_dir.mkdir(parents=True)
    (infra_dir / "docker-compose.infra.yml").write_text(render_infra_yaml(plan))
    (infra_dir / "docker-compose.apps.yml").write_text(render_apps_yaml(plan))
    (infra_dir / ".env.example").write_text(render_env_example(plan))
    (infra_dir / ".gitignore").write_text(".env\n")
    (infra_dir / "README.md").write_text(render_readme(plan))
    if preserved_env is not None:
        _merge_preserved_env(preserved_env, infra_dir / ".env.example", infra_dir / ".env")
        print("preserved existing infra/.env and appended newly required keys")

    if "postgres" in plan.infra_modules:
        pg = infra_dir / "pg-init"
        pg.mkdir()
        script = pg / "00-multi-db.sh"
        script.write_text(render_pg_init(plan))
        script.chmod(0o755)

    contracts = infra_dir / "contracts"
    contracts.mkdir()
    (contracts / "env.json").write_text(
        json.dumps(env_contract(plan), indent=2, sort_keys=True) + "\n"
    )
    (contracts / "postgres.json").write_text(
        json.dumps(postgres_contract(plan), indent=2, sort_keys=True) + "\n"
    )
    (contracts / "keycloak.json").write_text(
        json.dumps(plan.keycloak_spec, indent=2, sort_keys=True) + "\n"
    )
    (contracts / "kafka.json").write_text(
        json.dumps(plan.kafka_spec, indent=2, sort_keys=True) + "\n"
    )

    provision = infra_dir / "provision"
    provision.mkdir()
    if "keycloak" in plan.infra_modules:
        keycloak_script = provision / "keycloak.py"
        keycloak_script.write_text(render_keycloak_provisioner())
        keycloak_script.chmod(0o755)
    if "kafka" in plan.infra_modules:
        kafka_script = provision / "kafka.sh"
        kafka_script.write_text(render_kafka_provisioner(plan))
        kafka_script.chmod(0o755)

    df_dir = infra_dir / "dockerfiles"
    df_dir.mkdir()
    for s in plan.services:
        if s.dockerfile_source == "generated" and s.language == "node":
            (df_dir / f"Dockerfile.{s.name}").write_text(
                render_node_dockerfile(s)
            )

    print(f"\n✓ wrote {infra_dir}")
    print("next:")
    print(f"  cd {infra_dir.relative_to(Path.cwd()) if Path.cwd() in infra_dir.parents else infra_dir}")
    print("  # copy .env.example to .env, then run the idempotent bootstrap flow")
    print("  ../scripts/infra-up.sh --build")


def _ts() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------- Entrypoint ----------

def main() -> int:
    ap = argparse.ArgumentParser(prog="infra-init", description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=None, help="Project root (default: parent of scripts/)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing infra/ (backup made)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Use detected defaults and write without interactive prompts",
    )
    mode.add_argument(
        "--config",
        help="Use reviewed choices from a JSON config without interactive prompts",
    )
    mode.add_argument(
        "--detect-json",
        action="store_true",
        help="Print detected facts, suggested config, and uncertainties as JSON; write nothing",
    )
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    if not root.exists():
        print(f"error: root {root} not found", file=sys.stderr)
        return 2
    if args.detect_json:
        print(json.dumps(detection_report(root), indent=2, sort_keys=True))
        return 0
    print(f"project root: {root}\n")

    config = load_plan_config(Path(args.config).resolve()) if args.config else None
    plan = build_plan(root, args.force, assume_defaults=args.yes, config=config)

    # Summary
    print("\n=== plan ===")
    print(f"  project name  : {plan.project_name}")
    print(f"  network       : {plan.network_name}")
    print(f"  services      : {', '.join(f'{s.name}:{s.host_port}' for s in plan.services)}")
    print(f"  infra modules : {', '.join(plan.infra_modules) or '<none>'}")
    if not (args.yes or config is not None) and not ask_yn("\nwrite files?", True):
        print("aborted.")
        return 1

    write_plan(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
