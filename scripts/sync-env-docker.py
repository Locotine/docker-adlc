#!/usr/bin/env python3
"""Verify audited app env and its running local Docker dependencies.

The generated `infra/contracts/env.json` is authoritative: it is the union of
`.env.example`, production source configuration reads, validation schema and
Prisma env usage. Containers are resolved by Compose labels, so scaling and
Compose-generated names are supported.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from docker_contract import audit_service, is_placeholder  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA_SERVICES = [
    (r".*-postgres$", "postgres", 5432, "Postgres"),
    (r".*-redis$", "redis", 6379, "Redis"),
    (r".*-redpanda$", "redpanda", 29092, "Redpanda"),
    (r".*-keycloak$", "keycloak", 8080, "Keycloak"),
    (r".*-temporal$", "temporal", 7233, "Temporal"),
]
PLACEHOLDER_PAT = re.compile(
    r"REPLACE_ME|GENERATE_ME|your_|CHANGEME|<[A-Za-z0-9_ -]+>|xxx",
    re.IGNORECASE,
)
URL_SCHEME_PAT = re.compile(r"^(postgres(ql)?|redis|rediss|https?)://")
HOSTPORT_PAT = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*:\d+(?:,[A-Za-z][A-Za-z0-9_.-]*:\d+)*$")
BARE_HOSTPORT_KEYS = {"KAFKA_BROKERS", "KAFKA_BOOTSTRAP_SERVERS", "TEMPORAL_ADDRESS"}
BARE_HOST_KEYS = {"REDIS_HOST", "POSTGRES_HOST", "KAFKA_HOST", "TEMPORAL_HOST"}
DEFAULT_PLATFORM_ALLOWLIST = {
    "HOME", "HOSTNAME", "PATH", "PWD", "SHLVL", "NODE_VERSION", "YARN_VERSION",
}


def run_capture(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def sh(cmd: List[str]) -> str:
    result = run_capture(cmd)
    if result.returncode != 0:
        sys.stderr.write(f"[error] {' '.join(cmd)}\n{result.stderr}")
        raise RuntimeError(f"command failed ({result.returncode})")
    return result.stdout


def _compose_project_name() -> Optional[str]:
    compose = REPO_ROOT / "infra" / "docker-compose.infra.yml"
    if not compose.is_file():
        return None
    match = re.search(r"^name:\s*([^\s#]+)", compose.read_text(errors="replace"), re.MULTILINE)
    return match.group(1).strip("'\"") if match else None


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_service_contract(boundary: str) -> Dict[str, Any]:
    contract = _load_json(REPO_ROOT / "infra" / "contracts" / "env.json")
    services = contract.get("services", {})
    if isinstance(services, dict) and isinstance(services.get(boundary), dict):
        result = dict(services[boundary])
        result["platform_allowlist"] = contract.get("platform_allowlist", [])
        return result
    service_path = REPO_ROOT / boundary
    if not service_path.is_dir():
        return {}
    audit = audit_service(service_path, boundary)
    keys = [
        key for key, item in audit.env.items()
        if item.required or item.value is not None or any("schema" in source for source in item.sources)
    ]
    return {
        "required_keys": sorted(set(keys).union({"NODE_ENV", "PORT"})),
        "secret_keys": sorted(key for key, item in audit.env.items() if item.secret),
        "expected": {
            key: item.expected for key, item in audit.env.items() if item.expected
        },
        "health_path": audit.health_candidates[0] if audit.health_candidates else None,
        "secret_checks": [],
        "platform_allowlist": [],
    }


def load_env_example(boundary: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Compatibility API, now backed by the audited generated contract."""
    contract = load_service_contract(boundary)
    if contract:
        return (
            {key: "" for key in contract.get("required_keys", [])},
            dict(contract.get("expected", {})),
        )
    path = REPO_ROOT / boundary / ".env.example"
    if not path.is_file():
        sys.stderr.write(f"[error] missing contract and {path}\n")
        raise RuntimeError("missing env contract")
    schema: Dict[str, str] = {}
    expected: Dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        match = re.match(r"#\s*expected\s*(\S+)\s*=(.+)$", line)
        if match:
            expected[match.group(1)] = match.group(2).strip()
        elif line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            schema[key.strip()] = value.strip().strip("'\"")
    return schema, expected


def load_dotenv(path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("'\"")
    return result


def resolve_app_containers(boundary: str) -> List[str]:
    cmd = [
        "docker", "ps", "-a", "--format", "{{.Names}}",
        "--filter", f"label=com.docker.compose.service={boundary}",
        "--filter", "label=com.docker.compose.oneoff=False",
    ]
    project = _compose_project_name()
    if project:
        cmd += ["--filter", f"label=com.docker.compose.project={project}"]
    result = run_capture(cmd)
    if result.returncode == 0:
        containers = sorted(line for line in result.stdout.splitlines() if line)
        if containers:
            return containers
    # Backwards-compatible fallback for non-Compose/legacy containers whose
    # name is the service name; no project-specific boundary table is needed.
    return [boundary]


def docker_env(container: str) -> Dict[str, str]:
    envs = json.loads(sh(["docker", "inspect", "--format", "{{json .Config.Env}}", container]).strip())
    return dict(item.split("=", 1) for item in envs if "=" in item)


def container_project(container: str) -> Optional[str]:
    output = sh([
        "docker", "inspect", "--format",
        '{{index .Config.Labels "com.docker.compose.project"}}', container,
    ])
    return output.strip() or None


def container_health(container: str) -> Tuple[bool, str]:
    output = sh([
        "docker", "inspect", "--format",
        "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}", container,
    ]).strip()
    status, _, health = output.partition("|")
    effective = health or status
    return (health == "healthy" if health else status == "running"), effective


def _command_ports(inspect: Dict[str, Any]) -> Set[int]:
    config = inspect.get("Config", {}) or {}
    pieces = list(config.get("Entrypoint") or []) + list(config.get("Cmd") or [])
    text = " ".join(str(piece) for piece in pieces)
    return {
        int(value)
        for value in re.findall(r"(?:[A-Za-z][A-Za-z0-9+.-]*://)?(?:0\.0\.0\.0|localhost|[A-Za-z][A-Za-z0-9_.-]*):(\d{2,5})", text)
        if 1 <= int(value) <= 65535
    }


def discover_infra_map(project: Optional[str], exclude: Set[str]) -> Dict[str, List[Dict[str, Any]]]:
    args = ["docker", "ps", "--format", "{{.Names}}"]
    if project:
        args += ["--filter", f"label=com.docker.compose.project={project}"]
    result: Dict[str, List[Dict[str, Any]]] = {}
    for name in sh(args).splitlines():
        if not name or name in exclude:
            continue
        inspect = json.loads(sh(["docker", "inspect", name]))[0]
        env = dict(
            item.split("=", 1)
            for item in (inspect.get("Config", {}).get("Env", []) or [])
            if "=" in item
        )
        port_config = inspect.get("NetworkSettings", {}).get("Ports") or {}
        exposed = inspect.get("Config", {}).get("ExposedPorts") or {}
        internal_ports = {
            int(key.split("/")[0])
            for key in set(port_config).union(exposed)
            if key.split("/")[0].isdigit()
        }
        internal_ports.update(_command_ports(inspect))
        host_ports: Dict[int, int] = {}
        for key, bindings in port_config.items():
            internal = key.split("/")[0]
            if not internal.isdigit() or not bindings:
                continue
            for binding in bindings:
                host = binding.get("HostPort")
                if host and host.isdigit():
                    host_ports.setdefault(int(internal), int(host))
        aliases: Set[str] = {name}
        for network in (inspect.get("NetworkSettings", {}).get("Networks") or {}).values():
            aliases.update(network.get("Aliases") or [])
        labels = inspect.get("Config", {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if service:
            aliases.add(service)
        info = {
            "container": name,
            "service": service,
            "internal_ports": sorted(internal_ports),
            "host_ports": host_ports,
            "env": env,
            "image": inspect.get("Config", {}).get("Image"),
        }
        for alias in aliases:
            result.setdefault(alias, []).append(info)
    return result


def parse_pg_init(root: Path) -> Tuple[Dict[str, Dict[str, Any]], Set[str]]:
    contract = _load_json(root / "infra" / "contracts" / "postgres.json")
    databases = contract.get("databases")
    if isinstance(databases, list):
        users: Dict[str, Dict[str, Any]] = {}
        dbs: Set[str] = set()
        for database in databases:
            if not isinstance(database, dict):
                continue
            name, owner = database.get("name"), database.get("owner")
            if isinstance(name, str):
                dbs.add(name)
            if isinstance(owner, str):
                users.setdefault(owner, {"databases": set()})["databases"].add(name)
        return users, dbs
    users = {}
    dbs = set()
    pg_init = root / "infra" / "pg-init"
    if not pg_init.is_dir():
        return users, dbs
    ident = r'"?([\w-]+)"?'
    role_re = re.compile(
        rf"CREATE\s+(?:USER|ROLE)\s+{ident}[^;]*?PASSWORD\s+:?'([^']+)'",
        re.IGNORECASE | re.DOTALL,
    )
    db_re = re.compile(rf"CREATE\s+DATABASE\s+{ident}(?:\s+OWNER\s+{ident})?", re.IGNORECASE)
    helper_re = re.compile(r"ensure_role_and_db\s+([^\s#]+)\s+\S+\s+([^\s#]+)")
    for path in sorted(pg_init.iterdir()):
        if not path.is_file():
            continue
        content = path.read_text(errors="replace")
        for match in role_re.finditer(content):
            users.setdefault(match.group(1), {"databases": set()})
        for match in db_re.finditer(content):
            database, owner = match.group(1), match.group(2)
            dbs.add(database)
            if owner:
                users.setdefault(owner, {"databases": set()})["databases"].add(database)
        for match in helper_re.finditer(content):
            role, database = match.groups()
            if role.startswith('"') or role == '"$1"':
                continue
            dbs.add(database)
            users.setdefault(role, {"databases": set()})["databases"].add(database)
    return users, dbs


def probe_http(url: str, timeout: float = 3.0) -> Tuple[bool, int]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
            return 200 <= response.status < 400, response.status
    except urllib.error.HTTPError as error:
        return False, error.code
    except Exception:
        return False, 0


def _host_port_for(alias: str, internal_port: int, infra_map: Dict[str, List[Dict[str, Any]]]) -> Optional[int]:
    for container in infra_map.get(alias, []):
        value = container["host_ports"].get(internal_port)
        if value:
            return value
    return None


def _check_host_port(
    env_key: str,
    host: str,
    port: Optional[int],
    infra_map: Dict[str, List[Dict[str, Any]]],
    problems: List[Tuple[str, str, str]],
) -> None:
    if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        problems.append(("INFRA_HOST", env_key, f"container env points to loopback host {host!r}"))
        return
    if host not in infra_map:
        if "." not in host:
            problems.append((
                "INFRA_HOST", env_key,
                f"host {host!r} is not a Compose alias (known: {sorted(infra_map)})",
            ))
        return
    if port is not None and not any(port in item["internal_ports"] for item in infra_map[host]):
        available = sorted({value for item in infra_map[host] for value in item["internal_ports"]})
        problems.append((
            "INFRA_PORT", env_key,
            f"port {port} is not declared/listened by {host!r}; detected ports={available}",
        ))


def cross_check_infra(
    app_container: str,
    app_env: Dict[str, str],
    infra_map: Dict[str, List[Dict[str, Any]]],
    pg_users: Dict[str, Dict[str, Any]],
    pg_dbs: Set[str],
) -> List[Tuple[str, str, str]]:
    problems: List[Tuple[str, str, str]] = []
    for key, value in sorted(app_env.items()):
        value = value.strip()
        if not value:
            continue
        if URL_SCHEME_PAT.match(value):
            if "," in value:
                continue
            try:
                parsed = urlparse(value)
                host, port = parsed.hostname, parsed.port
            except ValueError:
                continue
            if not host:
                continue
            _check_host_port(key, host, port, infra_map, problems)
            if value.startswith(("postgresql://", "postgres://")) and parsed.username:
                if parsed.username not in pg_users and parsed.username != "postgres":
                    problems.append((
                        "INFRA_USER", key,
                        f"Postgres user {parsed.username!r} is absent from the generated contract",
                    ))
                database = (parsed.path or "").lstrip("/").split("?", 1)[0]
                if database and database not in pg_dbs and database != "postgres":
                    problems.append((
                        "INFRA_DB", key,
                        f"Postgres database {database!r} is absent from the generated contract",
                    ))
            if key.startswith("KEYCLOAK") and "/realms/" in value:
                realm_match = re.search(r"/realms/([^/]+)", value)
                host_port = _host_port_for(host, port or 8080, infra_map)
                if realm_match and host_port:
                    probe = (
                        f"{parsed.scheme}://localhost:{host_port}/realms/"
                        f"{realm_match.group(1)}/.well-known/openid-configuration"
                    )
                    ok, status = probe_http(probe)
                    if not ok:
                        problems.append((
                            "INFRA_REALM", key,
                            f"realm {realm_match.group(1)!r} discovery returned HTTP {status}",
                        ))
            continue
        if key in BARE_HOSTPORT_KEYS or HOSTPORT_PAT.fullmatch(value):
            for endpoint in value.split(","):
                host, _, raw_port = endpoint.strip().partition(":")
                _check_host_port(key, host, int(raw_port) if raw_port.isdigit() else None, infra_map, problems)
        elif key in BARE_HOST_KEYS and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", value):
            _check_host_port(key, value, None, infra_map, problems)
    return problems


def _allowed_extra(key: str, patterns: List[str]) -> bool:
    if key in DEFAULT_PLATFORM_ALLOWLIST or key.startswith(("NPM_", "npm_")):
        return True
    return any(fnmatch.fnmatchcase(key, pattern) for pattern in patterns)


def _secret_problems(
    actual: Dict[str, str], contract: Dict[str, Any], local_env: Dict[str, str]
) -> List[Tuple[str, str, str]]:
    problems: List[Tuple[str, str, str]] = []
    for check in contract.get("secret_checks", []):
        secret_env = check.get("secret_env")
        container_key = check.get("container_key")
        if secret_env not in local_env or container_key not in actual:
            continue
        expected = local_env[secret_env]
        observed = actual[container_key]
        if check.get("mode") == "url_password":
            try:
                observed = urlparse(observed).password or ""
            except ValueError:
                observed = ""
        if observed != expected:
            problems.append((
                "SECRET_DRIFT", container_key,
                f"container secret does not match infra/.env source {secret_env} (values hidden)",
            ))
    return problems


def _http_json(
    url: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    form: Optional[Dict[str, str]] = None,
    token: Optional[str] = None,
) -> Tuple[int, Any]:
    data = None
    headers: Dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        return error.code, None
    except Exception:
        return 0, None


def verify_keycloak_contract(
    infra_map: Dict[str, List[Dict[str, Any]]], local_env: Dict[str, str]
) -> List[Tuple[str, str, str]]:
    spec = _load_json(REPO_ROOT / "infra" / "contracts" / "keycloak.json")
    realms = spec.get("realms", [])
    if not realms:
        return []
    host_port = _host_port_for("keycloak", 8080, infra_map)
    if not host_port:
        return [("KEYCLOAK", "contract", "Keycloak host port is unavailable")]
    base = f"http://localhost:{host_port}"
    status, token_payload = _http_json(
        base + "/realms/master/protocol/openid-connect/token",
        method="POST",
        form={
            "client_id": "admin-cli",
            "username": "admin",
            "password": local_env.get("KEYCLOAK_ADMIN_PASSWORD", ""),
            "grant_type": "password",
        },
    )
    if status != 200 or not token_payload:
        return [("KEYCLOAK", "admin", f"admin token probe returned HTTP {status}")]
    token = token_payload["access_token"]
    problems: List[Tuple[str, str, str]] = []
    for realm in realms:
        name = realm["name"]
        rp = urllib.parse.quote(name, safe="")
        ok, discovery_status = probe_http(
            base + f"/realms/{rp}/.well-known/openid-configuration"
        )
        if not ok:
            problems.append(("KEYCLOAK_REALM", name, f"discovery returned HTTP {discovery_status}"))
            continue
        for role in realm.get("roles", []):
            status, _ = _http_json(
                base + f"/admin/realms/{rp}/roles/{urllib.parse.quote(role, safe='')}",
                token=token,
            )
            if status != 200:
                problems.append(("KEYCLOAK_ROLE", role, f"missing from realm {name}"))
        for client in realm.get("clients", []):
            query = urllib.parse.urlencode({"clientId": client["client_id"]})
            status, clients = _http_json(
                base + f"/admin/realms/{rp}/clients?{query}", token=token
            )
            if status != 200 or not clients:
                problems.append((
                    "KEYCLOAK_CLIENT", client["client_id"], f"missing from realm {name}",
                ))
                continue
            for role in client.get("roles", []):
                status, _ = _http_json(
                    base
                    + f"/admin/realms/{rp}/clients/{clients[0]['id']}/roles/"
                    + urllib.parse.quote(role, safe=""),
                    token=token,
                )
                if status != 200:
                    problems.append((
                        "KEYCLOAK_CLIENT_ROLE", role,
                        f"missing from client {client['client_id']} in realm {name}",
                    ))
            if client.get("kind") == "service-account":
                status, token_result = _http_json(
                    base + f"/realms/{rp}/protocol/openid-connect/token",
                    method="POST",
                    form={
                        "client_id": client["client_id"],
                        "client_secret": local_env.get(client["secret_env"], ""),
                        "grant_type": "client_credentials",
                    },
                )
                if status != 200 or not token_result or "access_token" not in token_result:
                    problems.append((
                        "KEYCLOAK_TOKEN", client["client_id"],
                        f"client_credentials probe returned HTTP {status}",
                    ))
    return problems


def verify_kafka_contract(
    infra_map: Dict[str, List[Dict[str, Any]]]
) -> List[Tuple[str, str, str]]:
    spec = _load_json(REPO_ROOT / "infra" / "contracts" / "kafka.json")
    expected = {
        topic["name"]: topic
        for topic in spec.get("topics", [])
        if isinstance(topic, dict) and isinstance(topic.get("name"), str)
    }
    if not expected and not spec.get("strict"):
        return []
    candidates = infra_map.get("redpanda", [])
    if not candidates:
        return [("KAFKA", "contract", "Redpanda container is unavailable")]
    container = candidates[0]["container"]
    problems: List[Tuple[str, str, str]] = []
    for name, topic in sorted(expected.items()):
        result = run_capture([
            "docker", "exec", container, "rpk", "-X", "brokers=redpanda:29092",
            "topic", "describe", name,
        ])
        if result.returncode != 0:
            problems.append(("KAFKA_TOPIC", name, "missing from Redpanda"))
            continue
        summary = re.search(
            r"(?m)^NAME\s+PARTITIONS\s+REPLICAS\s*$\n^\s*\S+\s+(\d+)\s+(\d+)\s*$",
            result.stdout,
        )
        if not summary:
            problems.append(("KAFKA_METADATA", name, "cannot parse rpk topic summary"))
            continue
        partitions, replicas = map(int, summary.groups())
        wanted_partitions = int(topic.get("partitions", 1))
        wanted_replicas = int(topic.get("replication_factor", 1))
        if partitions != wanted_partitions:
            problems.append((
                "KAFKA_PARTITIONS", name,
                f"expected {wanted_partitions}, observed {partitions}",
            ))
        if replicas != wanted_replicas:
            problems.append((
                "KAFKA_REPLICATION", name,
                f"expected {wanted_replicas}, observed {replicas}",
            ))
        observed_config: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[-1].endswith("_CONFIG"):
                observed_config[fields[0]] = fields[1]
        for key, value in sorted((topic.get("config") or {}).items()):
            if observed_config.get(key) != str(value):
                problems.append((
                    "KAFKA_CONFIG", f"{name}:{key}",
                    f"expected {value!r}, observed {observed_config.get(key)!r}",
                ))
    if spec.get("strict"):
        config = run_capture([
            "docker", "exec", container, "rpk", "-X", "brokers=redpanda:29092",
            "cluster", "config", "get", "auto_create_topics_enabled",
        ])
        if config.returncode != 0 or config.stdout.strip().lower() != "false":
            problems.append((
                "KAFKA_STRICT", "auto_create_topics_enabled",
                "expected false after explicit topic provisioning",
            ))
    return problems


def cmd_verify(boundary: str, skip_infra: bool = False) -> int:
    schema, expected_patterns = load_env_example(boundary)
    contract = load_service_contract(boundary)
    containers = resolve_app_containers(boundary)
    local_env = load_dotenv(REPO_ROOT / "infra" / ".env")
    all_problems: List[Tuple[str, str, str]] = []
    infra_map: Dict[str, List[Dict[str, Any]]] = {}
    project: Optional[str] = None

    for container in containers:
        actual = docker_env(container)
        problems: List[Tuple[str, str, str]] = []
        for key in schema:
            if key not in actual:
                problems.append(("MISSING", key, f"required by audited contract in {container}"))
                continue
            value = actual[key]
            if PLACEHOLDER_PAT.search(value) or is_placeholder(value):
                problems.append((
                    "PLACEHOLDER", key,
                    f"container value is placeholder-like (length={len(value)})",
                ))
            pattern = expected_patterns.get(key)
            if pattern and not re.fullmatch(pattern, value):
                problems.append((
                    "MISMATCH", key,
                    f"value length={len(value)} does not match expected pattern {pattern!r}",
                ))
        allowlist = list(contract.get("platform_allowlist", []))
        for key in actual:
            if key not in schema and not _allowed_extra(key, allowlist):
                problems.append(("EXTRA", key, "not declared by the audited env contract"))
        problems.extend(_secret_problems(actual, contract, local_env))
        healthy, health_status = container_health(container)
        if not healthy:
            problems.append(("HEALTH", container, f"container state is {health_status!r}"))
        if not skip_infra:
            project = container_project(container)
            infra_map = discover_infra_map(project, exclude={container})
            pg_users, pg_dbs = parse_pg_init(REPO_ROOT)
            problems.extend(cross_check_infra(container, actual, infra_map, pg_users, pg_dbs))
        print(f"=== verify {boundary} (container: {container}) ===")
        print(f"contract keys: {len(schema)}  actual keys: {len(actual)}  problems: {len(problems)}")
        for severity, key, message in sorted(problems):
            print(f"  [{severity:16s}] {key}: {message}")
        print()
        all_problems.extend(problems)

    if not skip_infra and infra_map:
        global_problems = verify_keycloak_contract(infra_map, local_env)
        global_problems.extend(verify_kafka_contract(infra_map))
        for severity, key, message in sorted(global_problems):
            print(f"  [{severity:16s}] {key}: {message}")
        if global_problems:
            print()
        all_problems.extend(global_problems)
    return 1 if all_problems else 0


def discover_host_ports() -> Dict[str, Tuple[str, int]]:
    result = run_capture(["docker", "ps", "--format", "{{json .}}"])
    if result.returncode != 0:
        return {}
    mapping: Dict[str, Tuple[str, int]] = {}
    for line in result.stdout.splitlines():
        row = json.loads(line)
        name, ports = row.get("Names", ""), row.get("Ports", "")
        for pattern, internal_host, internal_port, _ in INFRA_SERVICES:
            if not re.match(pattern, name):
                continue
            published = 9092 if internal_host == "redpanda" else internal_port
            match = re.search(rf"(?:0\.0\.0\.0|\[::\]):(\d+)->{published}/tcp", ports)
            if match:
                mapping[internal_host] = ("localhost", int(match.group(1)))
    return mapping


def cmd_gen_local(boundary: str, out_path: Optional[str]) -> int:
    example = REPO_ROOT / boundary / ".env.example"
    if not example.is_file():
        sys.stderr.write(f"[error] missing {example}\n")
        return 2
    infra = discover_host_ports()
    if not infra:
        sys.stderr.write("[error] no shared infra container detected\n")
        return 2
    # Resolve split HOST/PORT contracts before rewriting individual lines.  A
    # service such as Redis commonly declares REDIS_HOST=redis and
    # REDIS_PORT=6379 on separate lines; changing only the host would leave the
    # container port in a host-local env file.
    split_port_overrides: Dict[str, int] = {}
    example_lines = example.read_text().splitlines()
    for raw in example_lines:
        assignment = re.match(
            r"^\s*([A-Z][A-Z0-9_]*_HOST)\s*=\s*([A-Za-z][A-Za-z0-9_.-]*)\s*$",
            raw,
        )
        if not assignment:
            continue
        host_key, internal_host = assignment.groups()
        if internal_host in infra:
            split_port_overrides[host_key[:-5] + "_PORT"] = infra[internal_host][1]
    lines: List[str] = []
    substitutions = 0
    for raw in example_lines:
        line = raw
        port_assignment = re.match(r"^(\s*)([A-Z][A-Z0-9_]*_PORT)(\s*=\s*)\d+(\s*)$", line)
        if port_assignment and port_assignment.group(2) in split_port_overrides:
            line = (
                port_assignment.group(1)
                + port_assignment.group(2)
                + port_assignment.group(3)
                + str(split_port_overrides[port_assignment.group(2)])
                + port_assignment.group(4)
            )
            substitutions += line != raw
        for internal_host, (host, port) in infra.items():
            changed = re.sub(rf"\b{re.escape(internal_host)}:\d+", f"{host}:{port}", line)
            substitutions += changed != line
            line = changed
            host_match = re.match(
                rf"^([A-Z0-9_]*HOST[A-Z0-9_]*)\s*=\s*{re.escape(internal_host)}\s*$", line
            )
            if host_match:
                line = f"{host_match.group(1)}={host}"
                substitutions += 1
        lines.append(line)
    target = Path(out_path) if out_path else REPO_ROOT / boundary / ".env.local"
    target.write_text("\n".join(lines) + "\n")
    print(f"wrote {target} (substitutions: {substitutions})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="sync-env-docker", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="cmd", required=True)
    verify = commands.add_parser("verify", help="Strict audited contract and infra verification")
    verify.add_argument("boundary")
    verify.add_argument("--skip-infra", action="store_true")
    local = commands.add_parser("gen-local", help="Generate .env.local against published ports")
    local.add_argument("boundary")
    local.add_argument("--out", default=None)
    args = parser.parse_args()
    try:
        if args.cmd == "verify":
            return cmd_verify(args.boundary, args.skip_infra)
        return cmd_gen_local(args.boundary, args.out)
    except RuntimeError as error:
        sys.stderr.write(f"[error] {error}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
