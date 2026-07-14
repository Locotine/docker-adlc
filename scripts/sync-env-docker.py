#!/usr/bin/env python3
"""sync-env-docker — align boundary env against running Docker infra.

Three checks in `verify`:

  A. SCHEMA (self)           `.env.example` of the app  vs  env inside the app container
                              → MISSING / PLACEHOLDER / MISMATCH

  B. INFRA CROSS-CHECK        every URL-shaped env in the app container is dereferenced
                              against the SHARED INFRA containers in the same compose project.
                              → INFRA_HOST  (hostname in URL not an alias of any infra container)
                                INFRA_PORT  (port not exposed on that container)
                                INFRA_USER  (postgres user in DATABASE_URL not provisioned by pg-init/)
                                INFRA_DB    (postgres db in DATABASE_URL not provisioned)
                                INFRA_REALM (Keycloak realm in KEYCLOAK_*_URL not reachable via 200)

  C. EXTRA (info)             keys in the container not declared in `.env.example`.

`gen-local` remains unchanged: substitute `service:internal-port` → `localhost:host-port`
in `.env.example` and drop to `<boundary>/.env.local`.

Requires `docker` CLI + Python 3.8+ stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

# --- Boundary → container name mapping (điều chỉnh nếu naming đổi) ---
BOUNDARY_CONTAINER = {
    "d-bff-auth-client": "d-bff-auth-client",
    "d-identity-trust": "d-identity-trust",
    "d-taxonomy": "d-taxonomy",
}
REPO_ROOT = Path(__file__).resolve().parents[1]

# --- Legacy: static infra list used ONLY by gen-local for port discovery ---
INFRA_SERVICES = [
    (r"^dp-postgres$",  "postgres", 5432, "Postgres"),
    (r"^dp-redis$",     "redis",    6379, "Redis"),
    (r"^dp-redpanda$",  "redpanda", 29092, "Redpanda/Kafka (advertised internal 29092, host 9092)"),
    (r"^dp-keycloak$",  "keycloak", 8080, "Keycloak"),
    (r"^dp-temporal$",  "temporal", 7233, "Temporal"),
]

PLACEHOLDER_PAT = re.compile(r"REPLACE_ME|your_|CHANGEME|<[a-zA-Z_]+>|xxx", re.IGNORECASE)
URL_SCHEME_PAT  = re.compile(r"^(postgres(ql)?|redis|rediss|https?)://")
HOSTPORT_PAT    = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]*:\d+(?:,[a-zA-Z][a-zA-Z0-9_.-]*:\d+)*$")

# App env keys that carry hostnames but are NOT URLs — checked as bare host or host:port.
BARE_HOSTPORT_KEYS = {"KAFKA_BROKERS", "TEMPORAL_ADDRESS"}
BARE_HOST_KEYS     = {"REDIS_HOST", "POSTGRES_HOST", "KAFKA_HOST"}


# ---------- helpers ----------

def sh(cmd: List[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"[error] {' '.join(cmd)}\n{r.stderr}")
        sys.exit(r.returncode)
    return r.stdout


def load_env_example(boundary: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (schema_dict key→raw_value, expected_dict key→expected_pattern)."""
    p = REPO_ROOT / boundary / ".env.example"
    if not p.exists():
        sys.stderr.write(f"[error] missing {p}\n")
        sys.exit(2)
    schema: Dict[str, str] = {}
    expected: Dict[str, str] = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            m = re.match(r"#\s*expected\s*(\S+)\s*=(.+)$", line)
            if m:
                expected[m.group(1)] = m.group(2).strip()
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        schema[k.strip()] = v.strip().strip("'\"")
    return schema, expected


def docker_env(container: str) -> Dict[str, str]:
    out = sh(["docker", "inspect", "--format", "{{json .Config.Env}}", container])
    envs: List[str] = json.loads(out.strip())
    d: Dict[str, str] = {}
    for e in envs:
        if "=" in e:
            k, v = e.split("=", 1)
            d[k] = v
    return d


def container_project(container: str) -> Optional[str]:
    out = sh(["docker", "inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}', container])
    return out.strip() or None


def container_healthy(container: str) -> bool:
    """True if container's healthcheck says `healthy` (or no healthcheck and just Running)."""
    out = sh(["docker", "inspect", "--format", "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}", container]).strip()
    status, _, health = out.partition("|")
    if health:
        return health == "healthy"
    return status == "running"


# ---------- infra discovery ----------

def discover_infra_map(project: Optional[str], exclude: Set[str]) -> Dict[str, List[Dict]]:
    """Enumerate containers in compose project → map alias → list of {container, internal_ports, env, host_ports}.

    `exclude` = container names to skip (the app container itself + sibling apps, so we don't
    treat 'd-identity-trust' as an infra target when verifying 'd-bff-auth-client').
    """
    args = ["docker", "ps", "--format", "{{.Names}}"]
    if project:
        args += ["--filter", f"label=com.docker.compose.project={project}"]
    out = sh(args)
    result: Dict[str, List[Dict]] = {}
    for name in out.strip().splitlines():
        if not name or name in exclude:
            continue
        insp = json.loads(sh(["docker", "inspect", name]))[0]
        env = {}
        for e in insp.get("Config", {}).get("Env", []) or []:
            if "=" in e:
                k, v = e.split("=", 1)
                env[k] = v
        ports_cfg = insp.get("NetworkSettings", {}).get("Ports") or {}
        internal_ports = sorted({int(p.split("/")[0]) for p in ports_cfg if p.split("/")[0].isdigit()})
        # host-port map: {internal_port: host_port}
        host_ports: Dict[int, int] = {}
        for p, bindings in ports_cfg.items():
            digit = p.split("/")[0]
            if not digit.isdigit() or not bindings:
                continue
            for b in bindings:
                hp = b.get("HostPort")
                if hp and hp.isdigit():
                    host_ports.setdefault(int(digit), int(hp))
                    break
        aliases: Set[str] = {name}
        for net in (insp.get("NetworkSettings", {}).get("Networks") or {}).values():
            for a in (net.get("Aliases") or []):
                aliases.add(a)
        # compose service name
        svc = (insp.get("Config", {}).get("Labels") or {}).get("com.docker.compose.service")
        if svc:
            aliases.add(svc)
        info = {
            "container": name,
            "service": svc,
            "internal_ports": internal_ports,
            "host_ports": host_ports,
            "env": env,
            "image": insp.get("Config", {}).get("Image"),
        }
        for a in aliases:
            result.setdefault(a, []).append(info)
    return result


# ---------- pg-init parsing ----------

def parse_pg_init(root: Path) -> Tuple[Dict[str, Dict], Set[str]]:
    """Return (users_dict, all_databases_set). Handles:

    - `CREATE USER foo WITH PASSWORD 'bar'`
    - `CREATE ROLE "foo" LOGIN PASSWORD 'bar' [SUPERUSER]`
    - `CREATE DATABASE "foo" OWNER "bar"` (quoted or unquoted)
    - Bash helper: `create_role_and_db <role> <pass> <db>`
    """
    users: Dict[str, Dict] = {}
    dbs: Set[str] = set()
    pg_init = root / "infra" / "pg-init"
    if not pg_init.exists():
        return users, dbs

    ident = r'"?(\w+)"?'
    role_re = re.compile(
        rf"CREATE\s+(?:USER|ROLE)\s+{ident}[^;]*?PASSWORD\s+'([^']+)'",
        re.IGNORECASE | re.DOTALL,
    )
    db_re = re.compile(
        rf"CREATE\s+DATABASE\s+{ident}(?:\s+OWNER\s+{ident})?",
        re.IGNORECASE,
    )
    bash_re = re.compile(r"create_role_and_db\s+(\S+)\s+(\S+)\s+(\S+)")

    for f in sorted(pg_init.iterdir()):
        if not f.is_file():
            continue
        content = f.read_text()
        for m in role_re.finditer(content):
            users.setdefault(m.group(1), {"password": m.group(2), "databases": set()})
        for m in db_re.finditer(content):
            db, owner = m.group(1), m.group(2)
            dbs.add(db)
            if owner and owner in users:
                users[owner]["databases"].add(db)
        for m in bash_re.finditer(content):
            role, pw, db = m.group(1), m.group(2), m.group(3)
            users.setdefault(role, {"password": pw, "databases": set()})["databases"].add(db)
            dbs.add(db)
    return users, dbs


# ---------- URL probes ----------

def probe_http(url: str, timeout: float = 3.0) -> Tuple[bool, int]:
    """GET url. Return (ok, status). 200-399 = ok."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400, r.status
    except urllib.error.HTTPError as e:
        return False, e.code
    except Exception:
        return False, 0


# ---------- cross-check ----------

def cross_check_infra(
    app_container: str,
    app_env: Dict[str, str],
    infra_map: Dict[str, List[Dict]],
    pg_users: Dict[str, Dict],
    pg_dbs: Set[str],
) -> List[Tuple[str, str, str]]:
    problems: List[Tuple[str, str, str]] = []

    # Aggregate any POSTGRES_USER seen across infra as superuser candidates (per host).
    def superuser_for(host: str) -> Optional[str]:
        for c in infra_map.get(host, []):
            u = c["env"].get("POSTGRES_USER")
            if u:
                return u
        return None

    # Aggregate default POSTGRES_DB per host (extends pg_dbs set)
    def default_db_for(host: str) -> Optional[str]:
        for c in infra_map.get(host, []):
            d = c["env"].get("POSTGRES_DB")
            if d:
                return d
        return None

    for k, raw in sorted(app_env.items()):
        v = raw.strip()
        if not v:
            continue

        # --- URL-shaped env ---
        if URL_SCHEME_PAT.match(v):
            # Comma-separated multi-URL (e.g. CORS_ORIGINS) — treat as external, skip
            if "," in v:
                continue
            try:
                u = urlparse(v)
                host = u.hostname
                port = u.port
            except ValueError:
                continue
            if not host:
                continue
            _check_host_port(k, host, port, infra_map, problems)

            # Postgres user/db provisioning
            if v.startswith(("postgresql://", "postgres://")) and u.username:
                su = superuser_for(host)
                if u.username not in pg_users and u.username != su:
                    problems.append(("INFRA_USER", k,
                        f"pg user {u.username!r} not provisioned by pg-init/ (known: {sorted(pg_users)}) "
                        f"and != POSTGRES_USER superuser ({su!r})"))
                dbname = (u.path or "").lstrip("/").split("?")[0]
                if dbname:
                    dflt = default_db_for(host)
                    known_dbs = set(pg_dbs)
                    if dflt:
                        known_dbs.add(dflt)
                    if dbname not in known_dbs:
                        problems.append(("INFRA_DB", k,
                            f"pg db {dbname!r} not created by pg-init/ (known: {sorted(known_dbs)})"))

            # Keycloak realm existence probe
            if k.upper().startswith("KEYCLOAK") and "/realms/" in v:
                m = re.search(r"/realms/([^/]+)", v)
                if m:
                    realm = m.group(1)
                    # Reach Keycloak via host-port on localhost (Python outside compose network)
                    kc_port = port or (443 if u.scheme == "https" else 80)
                    hp = _host_port_for(host, kc_port, infra_map)
                    if hp:
                        probe = f"{u.scheme}://localhost:{hp}/realms/{realm}/.well-known/openid-configuration"
                        ok, status = probe_http(probe)
                        if not ok:
                            problems.append(("INFRA_REALM", k,
                                f"realm {realm!r} probe→{status} at {probe}"))
            continue

        # --- host:port list (KAFKA_BROKERS=redpanda:29092,other:29092) ---
        if k in BARE_HOSTPORT_KEYS or HOSTPORT_PAT.match(v):
            for broker in v.split(","):
                host, _, port_s = broker.strip().partition(":")
                port = int(port_s) if port_s.isdigit() else None
                _check_host_port(k, host, port, infra_map, problems)
            continue

        # --- bare host key ---
        if k in BARE_HOST_KEYS and re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", v):
            _check_host_port(k, v, None, infra_map, problems)

    return problems


def _check_host_port(env_key: str, host: str, port: Optional[int],
                     infra_map: Dict[str, List[Dict]], problems: List[Tuple[str, str, str]]) -> None:
    if host in ("localhost", "127.0.0.1", "0.0.0.0"):
        return
    if host not in infra_map:
        # Ignore FQDN-looking external hosts (dots) — probably real DNS
        if "." in host:
            return
        problems.append(("INFRA_HOST", env_key,
            f"host {host!r} not an alias of any infra container in project "
            f"(known aliases: {sorted(a for a in infra_map if not a.startswith('/'))})"))
        return
    if port is None:
        return
    if not any(port in c["internal_ports"] for c in infra_map[host]):
        avail = sorted({p for c in infra_map[host] for p in c["internal_ports"]})
        cnames = [c["container"] for c in infra_map[host]]
        # Downgrade to HINT: docker inspect only tracks EXPOSE'd ports. Container may still
        # listen on the port (e.g. redpanda advertised-kafka-addr 29092). If the app is
        # actually failing to connect the healthcheck will show that instead.
        problems.append(("INFRA_PORT_HINT", env_key,
            f"port {port} not in EXPOSE'd ports of {host!r} → {cnames} internal ports={avail}. "
            f"May still listen internally (advertised-only)."))


def _host_port_for(alias: str, internal_port: int,
                   infra_map: Dict[str, List[Dict]]) -> Optional[int]:
    for c in infra_map.get(alias, []):
        hp = c["host_ports"].get(internal_port)
        if hp:
            return hp
    return None


# ---------- commands ----------

def cmd_verify(boundary: str, skip_infra: bool = False) -> int:
    container = BOUNDARY_CONTAINER.get(boundary, boundary)
    schema, expected = load_env_example(boundary)
    actual = docker_env(container)

    problems: List[Tuple[str, str, str]] = []

    # A. Schema self-check
    for k, _schema_v in schema.items():
        if k not in actual:
            problems.append(("MISSING", k, f"required by .env.example, not injected in {container}"))
            continue
        av = actual[k]
        if PLACEHOLDER_PAT.search(av) or av in ("REPLACE_ME_ON_IMPORT",):
            problems.append(("PLACEHOLDER", k,
                f"container value is still placeholder-like (length={len(av)})"))
        if k in expected:
            pat = expected[k]
            if not re.fullmatch(pat, av):
                problems.append(("MISMATCH", k,
                    f"container value does not match expected pattern "
                    f"(length={len(av)}; expected~{pat!r})"))

    extras = [k for k in actual if k not in schema
              and not k.startswith(("PATH", "HOME", "HOSTNAME", "NODE_", "NPM_", "YARN_"))]

    # B. Infra cross-check
    infra_problems: List[Tuple[str, str, str]] = []
    infra_summary_lines: List[str] = []
    if not skip_infra:
        project = container_project(container)
        infra_map = discover_infra_map(project, exclude={container})
        pg_users, pg_dbs = parse_pg_init(REPO_ROOT)
        infra_problems = cross_check_infra(container, actual, infra_map, pg_users, pg_dbs)
        # summary
        infra_containers = {c["container"] for lst in infra_map.values() for c in lst}
        infra_summary_lines.append(
            f"infra scope: project={project!r} → {len(infra_containers)} sibling containers "
            f"({sorted(infra_containers)})"
        )
        if pg_users:
            infra_summary_lines.append(f"pg-init users: {sorted(pg_users)}   pg-init dbs: {sorted(pg_dbs)}")
        problems.extend(infra_problems)

    # ---- print ----
    print(f"=== verify {boundary} (container: {container}) ===")
    for line in infra_summary_lines:
        print(f"  {line}")
    print(f"schema keys: {len(schema)}  actual keys: {len(actual)}  problems: {len(problems)}\n")

    for sev, k, msg in sorted(problems, key=lambda x: (x[0], x[1])):
        print(f"  [{sev:12s}] {k}: {msg}")
    if extras:
        print(f"\n  [EXTRA (info)] {len(extras)} keys in container not declared in .env.example:")
        for k in sorted(extras)[:20]:
            print(f"                  - {k}")
        if len(extras) > 20:
            print(f"                  ... (+{len(extras)-20} more)")
    print()
    hard = {"MISSING", "PLACEHOLDER", "MISMATCH", "INFRA_HOST", "INFRA_USER", "INFRA_DB", "INFRA_REALM"}
    return 1 if any(sev in hard for sev, _, _ in problems) else 0


def discover_host_ports() -> Dict[str, Tuple[str, int]]:
    """Legacy helper for gen-local — map static internal-host → (localhost, host-port)."""
    out = sh(["docker", "ps", "--format", "{{json .}}"])
    mapping: Dict[str, Tuple[str, int]] = {}
    for line in out.splitlines():
        row = json.loads(line)
        name = row.get("Names", "")
        ports_str = row.get("Ports", "")
        for pat, internal_host, internal_port, _desc in INFRA_SERVICES:
            if re.match(pat, name):
                m = re.search(rf"0\.0\.0\.0:(\d+)->{internal_port}/tcp", ports_str)
                if not m and internal_host == "redpanda":
                    m = re.search(r"0\.0\.0\.0:(\d+)->9092/tcp", ports_str)
                if m:
                    mapping[internal_host] = ("localhost", int(m.group(1)))
    return mapping


def cmd_gen_local(boundary: str, out_path: Optional[str]) -> int:
    example = REPO_ROOT / boundary / ".env.example"
    if not example.exists():
        sys.stderr.write(f"[error] missing {example}\n")
        return 2
    infra = discover_host_ports()
    if not infra:
        sys.stderr.write("[error] no shared infra container detected. Is `docker compose -f infra/docker-compose.infra.yml up -d` running?\n")
        return 2

    lines: List[str] = []
    subs = 0
    for raw in example.read_text().splitlines():
        line = raw
        for internal_host, (host, port) in infra.items():
            pat_hostport = re.compile(rf"\b{re.escape(internal_host)}:\d+")
            new = pat_hostport.sub(f"{host}:{port}", line)
            if new != line:
                subs += 1
            line = new
            m = re.match(rf"^([A-Z0-9_]*HOST[A-Z0-9_]*)\s*=\s*{re.escape(internal_host)}\s*$", line)
            if m:
                line = f"{m.group(1)}={host}"
                subs += 1
        lines.append(line)

    target = Path(out_path) if out_path else REPO_ROOT / boundary / ".env.local"
    target.write_text("\n".join(lines) + "\n")
    print(f"wrote {target}  (substitutions: {subs})")
    print("Detected infra host-port mapping:")
    for h, (_, p) in sorted(infra.items()):
        print(f"  {h} → localhost:{p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="sync-env-docker", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="Check container env against .env.example AND cross-check infra")
    v.add_argument("boundary", choices=sorted(BOUNDARY_CONTAINER.keys()))
    v.add_argument("--skip-infra", action="store_true",
                   help="Skip cross-check against sibling infra containers")

    g = sub.add_parser("gen-local", help="Generate .env.local for running boundary locally against Docker infra")
    g.add_argument("boundary", choices=sorted(BOUNDARY_CONTAINER.keys()))
    g.add_argument("--out", default=None, help="Output path (default: <boundary>/.env.local)")

    args = ap.parse_args()
    if args.cmd == "verify":
        return cmd_verify(args.boundary, args.skip_infra)
    if args.cmd == "gen-local":
        return cmd_gen_local(args.boundary, args.out)
    return 2


if __name__ == "__main__":
    sys.exit(main())
