#!/usr/bin/env python3
"""infra-init — scaffold shared Docker infra for a multi-service project.

Interactive workflow:
  1. Detect candidate service folders (dir với package.json / go.mod / pyproject.toml).
  2. Parse tech-stack hints (deps in package.json, imports) to guess needed infra:
        prisma|pg|typeorm         → postgres
        redis|ioredis|bullmq       → redis
        kafkajs|@confluentinc      → redpanda (kafka)
        openid-client|jose|passport-jwt|keycloak-* → keycloak
        @temporalio/*              → temporal
        @aws-sdk/client-s3|minio   → minio
        @elastic/elasticsearch     → elasticsearch
  3. Prompt user to confirm services, port mapping, compose project name.
  4. Prompt to enable/disable each detected infra module.
  5. Generate `infra/` next to the services:
        infra/docker-compose.infra.yml
        infra/docker-compose.apps.yml
        infra/dockerfiles/Dockerfile.<service>          (only for Node services without Dockerfile)
        infra/.env.example
        infra/.gitignore
        infra/README.md
        infra/pg-init/00-multi-db.sh                     (if postgres)
        infra/keycloak-realms/README.md                  (if keycloak — hint drop realm JSON here)

Not destructive by default — refuses to overwrite existing infra/ unless `--force`.
Prints a diff-friendly plan before writing.

Requires: Python 3.8+. No external deps.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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


def build_plan(root: Path, force: bool) -> InitPlan:
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
    default_name = root.name.lower().replace(" ", "-")
    project_name = ask("Compose project name (docker -p)", default_name)
    network_name = ask("Docker network name", project_name)

    # 2) per-service confirm + port
    print("\n--- select services to include ---")
    next_port = 4000
    for s in cands:
        s.include = ask_yn(f"include '{s.name}'?", True)
        if s.include:
            default_port = s.detected_port or next_port
            s.host_port = ask_int(f"  host port for {s.name}", default_port)
            s.container_port = ask_int(f"  container port for {s.name}", s.detected_port or 3000)
            next_port = s.host_port + 1

    included = [s for s in cands if s.include]
    if not included:
        print("no services selected. abort.")
        sys.exit(1)

    # 3) aggregate detected infra + prompt each
    aggregate = set()
    for s in included:
        aggregate.update(s.detected_infra)
    print("\n--- infra modules ---")
    print(f"  auto-detected from tech stack: {sorted(aggregate) or 'none'}")
    modules: List[str] = []
    for m in INFRA_MODULES_ALL:
        default = m in aggregate
        if ask_yn(f"  enable '{m}'?", default):
            modules.append(m)

    return InitPlan(
        project_root=root,
        project_name=project_name,
        network_name=network_name,
        services=included,
        infra_modules=modules,
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
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: postgres
    ports: ['5432:5432']
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
    ports: ['6379:6379']
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
      - --advertise-kafka-addr=PLAINTEXT://redpanda:29092,OUTSIDE://localhost:9092
      - --pandaproxy-addr=0.0.0.0:8082
      - --advertise-pandaproxy-addr=localhost:8082
    ports:
      - '9092:9092'
      - '8082:8082'
      - '9644:9644'
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
    command: [start-dev, --http-port=8080, --hostname-strict=false, --import-realm]
    environment:
      KEYCLOAK_ADMIN: admin
      KEYCLOAK_ADMIN_PASSWORD: admin
      KC_DB: postgres
      KC_DB_URL: jdbc:postgresql://postgres:5432/keycloak
      KC_DB_USERNAME: keycloak
      KC_DB_PASSWORD: keycloak
      KC_HEALTH_ENABLED: 'true'
      KC_HTTP_ENABLED: 'true'
      KC_HOSTNAME_STRICT: 'false'
    ports: ['8080:8080']
    depends_on: {{ postgres: {{ condition: service_healthy }} }}
    volumes:
      - keycloak-data:/opt/keycloak/data
      - ./keycloak-realms:/opt/keycloak/data/import:ro
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
      POSTGRES_PWD: temporal
      POSTGRES_SEEDS: postgres
      DBNAME: temporal
      VISIBILITY_DBNAME: temporal_visibility
      DEFAULT_NAMESPACE: default
      DEFAULT_NAMESPACE_RETENTION: 24h
    ports: ['7233:7233']
    depends_on: {{ postgres: {{ condition: service_healthy }} }}
    healthcheck:
      test: ['CMD-SHELL', 'temporal operator cluster health || true']
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
    ports: ['8233:8080']
    depends_on: [temporal]
    networks: [{net}]
""",
    "minio": """  minio:
    image: minio/minio:latest
    container_name: {proj}-minio
    restart: unless-stopped
    command: [server, /data, --console-address, ':9001']
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports: ['9000:9000', '9001:9001']
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
    ports: ['9200:9200']
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

DOCKERFILE_NODE = """# Auto-generated by infra-init. Adjust if project has non-standard build.
FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci

FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
RUN apk add --no-cache curl tini
COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package*.json ./
EXPOSE {port}
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \\
  CMD curl -fsS http://localhost:{port}/health || exit 1
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "dist/main.js"]
"""


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
    body = ""
    for m in plan.infra_modules:
        body += INFRA_SERVICE_YAML[m].format(proj=plan.project_name, net=plan.network_name)
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
        df_path = f"../infra/dockerfiles/Dockerfile.{s.name}" if not s.has_dockerfile else "Dockerfile"
        env_lines = _env_for_service(s, plan)
        env_block = "".join(f"      {k}: {_yaml_quote(v)}\n" for k, v in env_lines)
        depends_on = _depends_on(plan)
        body += f"""  {s.name}:
    build:
      context: ../{s.name}
      dockerfile: {df_path}
    image: {plan.project_name}/{s.name}:local
    container_name: {s.name}
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


def _env_for_service(s: ServiceCandidate, plan: InitPlan) -> List[Tuple[str, str]]:
    env: List[Tuple[str, str]] = []
    if "postgres" in s.detected_infra and "postgres" in plan.infra_modules:
        env.append(("DATABASE_URL", f"postgresql://{s.name}:{s.name}@postgres:5432/{s.name}"))
    if "redis" in s.detected_infra and "redis" in plan.infra_modules:
        env.append(("REDIS_URL", "redis://redis:6379"))
    if "kafka" in s.detected_infra and "kafka" in plan.infra_modules:
        env.append(("KAFKA_BROKERS", "redpanda:29092"))
        env.append(("KAFKA_CLIENT_ID", s.name))
    if "keycloak" in s.detected_infra and "keycloak" in plan.infra_modules:
        env.append(("KEYCLOAK_URL", "http://keycloak:8080"))
    if "temporal" in s.detected_infra and "temporal" in plan.infra_modules:
        env.append(("TEMPORAL_ADDRESS", "temporal:7233"))
    if "minio" in s.detected_infra and "minio" in plan.infra_modules:
        env.append(("S3_ENDPOINT", "http://minio:9000"))
        env.append(("S3_ACCESS_KEY_ID", "minioadmin"))
        env.append(("S3_SECRET_ACCESS_KEY", "minioadmin"))
    if "elasticsearch" in s.detected_infra and "elasticsearch" in plan.infra_modules:
        env.append(("ELASTICSEARCH_URL", "http://elasticsearch:9200"))
    return env


def _depends_on(plan: InitPlan) -> str:
    conds = []
    for m in plan.infra_modules:
        host = MODULE_HOST[m]
        if m == "temporal":
            conds.append(f"      {host}:\n        condition: service_started")
        elif m in ("postgres", "redis", "kafka", "keycloak", "minio", "elasticsearch"):
            conds.append(f"      {host}:\n        condition: service_healthy")
    return "\n".join(conds) if conds else "      {}"


def render_env_example(plan: InitPlan) -> str:
    lines = [
        "# Copy to `.env` (KHÔNG commit) và điền secret thật.",
        "# Các biến ${VAR:?...} trong docker-compose.apps.yml sẽ đọc từ file này.",
        "",
    ]
    if "keycloak" in plan.infra_modules:
        lines += [
            "# Mint client secret sau khi Keycloak up (thay dp-p2 = realm của bạn):",
            "#   TOKEN=$(curl -s -X POST http://localhost:8080/realms/master/protocol/openid-connect/token \\",
            "#     -d client_id=admin-cli -d username=admin -d password=admin -d grant_type=password \\",
            "#     | python3 -c 'import json,sys;print(json.load(sys.stdin)[\"access_token\"])')",
            "#   CID=$(curl -s -H \"Authorization: Bearer $TOKEN\" \\",
            "#     'http://localhost:8080/admin/realms/<REALM>/clients?clientId=<CLIENT>' \\",
            "#     | python3 -c 'import json,sys;print(json.load(sys.stdin)[0][\"id\"])')",
            "#   curl -s -X POST -H \"Authorization: Bearer $TOKEN\" \\",
            "#     \"http://localhost:8080/admin/realms/<REALM>/clients/$CID/client-secret\"",
            "",
            "# EXAMPLE_ADMIN_SECRET=REPLACE_ME",
            "",
        ]
    return "\n".join(lines)


def render_pg_init(plan: InitPlan) -> str:
    dbs = [s.name for s in plan.services if "postgres" in s.detected_infra]
    body = "#!/bin/bash\n# Auto-generated. Creates one DB + role per service.\nset -e\n\n"
    for db in dbs:
        body += f'psql -v ON_ERROR_STOP=1 --username postgres <<-EOSQL\n'
        body += f'    CREATE USER {db} WITH PASSWORD \'{db}\';\n'
        body += f'    CREATE DATABASE {db} OWNER {db};\n'
        body += f'    GRANT ALL ON DATABASE {db} TO {db};\n'
        body += "EOSQL\n\n"
    # Extra DBs for keycloak/temporal
    if "keycloak" in plan.infra_modules:
        body += 'psql -v ON_ERROR_STOP=1 --username postgres <<-EOSQL\n'
        body += "    CREATE USER keycloak WITH PASSWORD 'keycloak';\n"
        body += "    CREATE DATABASE keycloak OWNER keycloak;\nEOSQL\n\n"
    if "temporal" in plan.infra_modules:
        body += 'psql -v ON_ERROR_STOP=1 --username postgres <<-EOSQL\n'
        body += "    CREATE USER temporal WITH PASSWORD 'temporal';\n"
        body += "    CREATE DATABASE temporal OWNER temporal;\n"
        body += "    CREATE DATABASE temporal_visibility OWNER temporal;\nEOSQL\n"
    return body


def render_readme(plan: InitPlan) -> str:
    svc_rows = "\n".join(
        f"| {s.name} | {s.host_port} | {s.container_port} |" for s in plan.services
    )
    infra_rows = "\n".join(
        f"| {plan.project_name}-{MODULE_HOST[m]} | shared |" for m in plan.infra_modules
    )
    return f"""# {plan.project_name} — Shared Infra

Auto-scaffolded by `scripts/infra-init.py`.

## Bring up

```bash
cd infra
docker compose -f docker-compose.infra.yml up -d
# after Keycloak/Postgres healthy, mint secrets → .env
docker compose -f docker-compose.infra.yml -f docker-compose.apps.yml up -d --build
```

Or use wrappers:
- `../scripts/infra-up.sh` — up all (infra + apps)
- `../scripts/infra-up.sh --infra-only` — just infra
- `../scripts/docker-apps-up.sh [service...]` — apps only, infra assumed healthy
- `../scripts/infra-down.sh [--volumes]` — down; add `--volumes` to wipe data

## App port map

| Service | Host | Container |
|---------|------|-----------|
{svc_rows}

## Infra services

| Container | Notes |
|-----------|-------|
{infra_rows}

## Verifying env

```bash
../scripts/sync-env-docker.py verify <service>       # if wired into scripts/
# or from anywhere via skill: /sync-env-docker
```
"""


# ---------- Write ----------

def write_plan(plan: InitPlan) -> None:
    infra_dir = plan.project_root / "infra"
    if infra_dir.exists() and not plan.force:
        print(f"\nerror: {infra_dir} already exists. Use --force to overwrite.")
        sys.exit(1)
    if infra_dir.exists() and plan.force:
        backup = plan.project_root / f"infra.backup.{_ts()}"
        print(f"backing up existing infra/ → {backup.name}/")
        shutil.move(str(infra_dir), str(backup))

    infra_dir.mkdir(parents=True)
    (infra_dir / "docker-compose.infra.yml").write_text(render_infra_yaml(plan))
    (infra_dir / "docker-compose.apps.yml").write_text(render_apps_yaml(plan))
    (infra_dir / ".env.example").write_text(render_env_example(plan))
    (infra_dir / ".gitignore").write_text(".env\n")
    (infra_dir / "README.md").write_text(render_readme(plan))

    if "postgres" in plan.infra_modules:
        pg = infra_dir / "pg-init"
        pg.mkdir()
        script = pg / "00-multi-db.sh"
        script.write_text(render_pg_init(plan))
        script.chmod(0o755)

    if "keycloak" in plan.infra_modules:
        kr = infra_dir / "keycloak-realms"
        kr.mkdir()
        (kr / "README.md").write_text(
            "# Realm exports\n\nDrop realm JSON files here (e.g. `dp-p1-realm.json`).\n"
            "`--import-realm` in docker-compose will auto-import on Keycloak start.\n"
        )

    df_dir = infra_dir / "dockerfiles"
    df_dir.mkdir()
    for s in plan.services:
        if not s.has_dockerfile and s.language == "node":
            (df_dir / f"Dockerfile.{s.name}").write_text(
                DOCKERFILE_NODE.format(port=s.container_port)
            )

    print(f"\n✓ wrote {infra_dir}")
    print("next:")
    print(f"  cd {infra_dir.relative_to(Path.cwd()) if Path.cwd() in infra_dir.parents else infra_dir}")
    print("  # edit .env (mint secrets)")
    print("  docker compose -f docker-compose.infra.yml up -d")


def _ts() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------- Entrypoint ----------

def main() -> int:
    ap = argparse.ArgumentParser(prog="infra-init", description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=None, help="Project root (default: parent of scripts/)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing infra/ (backup made)")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    if not root.exists():
        print(f"error: root {root} not found", file=sys.stderr)
        return 2
    print(f"project root: {root}\n")

    plan = build_plan(root, args.force)

    # Summary
    print("\n=== plan ===")
    print(f"  project name  : {plan.project_name}")
    print(f"  network       : {plan.network_name}")
    print(f"  services      : {', '.join(f'{s.name}:{s.host_port}' for s in plan.services)}")
    print(f"  infra modules : {', '.join(plan.infra_modules) or '<none>'}")
    if not ask_yn("\nwrite files?", True):
        print("aborted.")
        return 1

    write_plan(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
