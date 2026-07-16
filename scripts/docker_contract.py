#!/usr/bin/env python3
"""Shared source-contract discovery for docker-claude generators and verifiers.

The module intentionally uses only Python's standard library because it is copied
into consumer repositories and must work before project dependencies are installed.
It extracts evidence; callers remain responsible for asking users about ambiguous
or missing values instead of inventing product configuration.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


DEFAULT_NODE_MAJOR = 24
SOURCE_SUFFIXES = {".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"}
IGNORED_SOURCE_PARTS = {
    ".git",
    ".next",
    "build",
    "coverage",
    "dist",
    "fixtures",
    "generated",
    "mocks",
    "node_modules",
    "test",
    "tests",
}
TEST_FILE_RE = re.compile(r"(?:^|[._-])(spec|test|mock|fixture)(?:[._-]|$)", re.IGNORECASE)
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.v\d+$")
SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:PASSWORD|PASS|SECRET|TOKEN|PRIVATE_KEY|API_KEY|ACCESS_KEY)(?:_|$)",
    re.IGNORECASE,
)
PLACEHOLDER_RE = re.compile(
    r"REPLACE_ME|GENERATE_ME|CHANGEME|your_|<[A-Za-z0-9_ -]+>|xxx",
    re.IGNORECASE,
)


@dataclass
class EnvEvidence:
    key: str
    value: Optional[str] = None
    required: bool = False
    secret: bool = False
    sources: Set[str] = field(default_factory=set)
    expected: Optional[str] = None
    has_code_default: bool = False

    def merge(self, other: "EnvEvidence") -> None:
        if other.value is not None:
            self.value = other.value
        self.required = self.required or other.required
        self.secret = self.secret or other.secret
        self.sources.update(other.sources)
        self.expected = other.expected or self.expected
        self.has_code_default = self.has_code_default or other.has_code_default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "required": self.required,
            "secret": self.secret,
            "sources": sorted(self.sources),
            "expected": self.expected,
            "has_code_default": self.has_code_default,
        }


@dataclass
class ServiceAudit:
    name: str
    env: Dict[str, EnvEvidence] = field(default_factory=dict)
    env_example_keys: Set[str] = field(default_factory=set)
    code_env_keys: Set[str] = field(default_factory=set)
    node_major: int = DEFAULT_NODE_MAJOR
    node_engine: Optional[str] = None
    start_script: Optional[str] = None
    build_script: Optional[str] = None
    prisma: bool = False
    prisma_schemas: List[str] = field(default_factory=list)
    health_candidates: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    realm_access_claim: bool = False
    resource_access_claim: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "env": {key: value.to_dict() for key, value in sorted(self.env.items())},
            "env_example_keys": sorted(self.env_example_keys),
            "code_env_keys": sorted(self.code_env_keys),
            "node_major": self.node_major,
            "node_engine": self.node_engine,
            "start_script": self.start_script,
            "build_script": self.build_script,
            "prisma": self.prisma,
            "prisma_schemas": self.prisma_schemas,
            "health_candidates": self.health_candidates,
            "topics": self.topics,
            "realm_access_claim": self.realm_access_claim,
            "resource_access_claim": self.resource_access_claim,
        }


def is_secret_key(key: str) -> bool:
    return SECRET_KEY_RE.search(key) is not None


def is_placeholder(value: Optional[str]) -> bool:
    return bool(value is not None and PLACEHOLDER_RE.search(value))


def parse_env_example(path: Path) -> Tuple[Dict[str, EnvEvidence], Dict[str, str]]:
    """Parse an env example and adjacent `# expected KEY=regex` declarations."""
    evidence: Dict[str, EnvEvidence] = {}
    expected: Dict[str, str] = {}
    if not path.is_file():
        return evidence, expected
    for raw in path.read_text(errors="replace").splitlines():
        stripped = raw.strip()
        match = re.match(r"#\s*expected\s+([A-Z][A-Z0-9_]*)\s*=\s*(.+)$", stripped)
        if match:
            expected[match.group(1)] = match.group(2).strip()
            continue
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not ENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip().strip("'\"")
        evidence[key] = EnvEvidence(
            key=key,
            value=value,
            required=True,
            secret=is_secret_key(key),
            sources={"env_example"},
            expected=expected.get(key),
        )
    for key, pattern in expected.items():
        if key in evidence:
            evidence[key].expected = pattern
    return evidence, expected


def _strip_js_comments(content: str) -> str:
    """Remove JS/TS comments while preserving quoted strings and line positions."""
    out: List[str] = []
    index = 0
    quote: Optional[str] = None
    escaped = False
    while index < len(content):
        char = content[index]
        nxt = content[index + 1] if index + 1 < len(content) else ""
        if quote:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and nxt == "/":
            while index < len(content) and content[index] != "\n":
                out.append(" ")
                index += 1
            continue
        if char == "/" and nxt == "*":
            out.extend((" ", " "))
            index += 2
            while index < len(content):
                if content[index:index + 2] == "*/":
                    out.extend((" ", " "))
                    index += 2
                    break
                out.append("\n" if content[index] == "\n" else " ")
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def iter_production_sources(service_path: Path) -> Iterable[Path]:
    src = service_path / "src"
    if not src.is_dir():
        return []
    result: List[Path] = []
    for path in src.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(src)
        if any(part.lower() in IGNORED_SOURCE_PARTS for part in relative.parts):
            continue
        if TEST_FILE_RE.search(path.name):
            continue
        result.append(path)
    return sorted(result)


CONFIG_CALL_RE = re.compile(
    r"\.(getOrThrow|get)(?:\s*<[^>]*>)?\s*\(\s*(['\"])([A-Z][A-Z0-9_]*)\2"
    r"(?:\s*,\s*([^\n\r\)]*))?",
    re.MULTILINE,
)
PROCESS_ENV_RE = re.compile(
    r"process\.env(?:\.([A-Z][A-Z0-9_]*)|\[['\"]([A-Z][A-Z0-9_]*)['\"]\])"
)
SCHEMA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]*)\s*:\s*(Joi|z)\.", re.MULTILINE)
STRING_RE = re.compile(r"(['\"])([^'\"\r\n]+)\1")


def _literal_default(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    value = raw.strip()
    match = re.match(r"(['\"])(.*?)\1\s*$", value)
    if match:
        return match.group(2)
    if re.fullmatch(r"-?\d+(?:\.\d+)?|true|false", value, re.IGNORECASE):
        return value
    return None


def scan_code_contract(service_path: Path) -> Tuple[Dict[str, EnvEvidence], Set[str], bool, bool]:
    env: Dict[str, EnvEvidence] = {}
    topics: Set[str] = set()
    realm_access = False
    resource_access = False
    for source in iter_production_sources(service_path):
        content = _strip_js_comments(source.read_text(errors="replace"))
        relative = source.relative_to(service_path).as_posix()
        realm_access = realm_access or "realm_access" in content
        resource_access = resource_access or "resource_access" in content
        for match in CONFIG_CALL_RE.finditer(content):
            method, key, raw_default = match.group(1), match.group(3), match.group(4)
            default = _literal_default(raw_default)
            item = EnvEvidence(
                key=key,
                value=default,
                required=method == "getOrThrow" and default is None,
                secret=is_secret_key(key),
                sources={f"code:{relative}:{method}"},
                has_code_default=default is not None,
            )
            env.setdefault(key, EnvEvidence(key=key)).merge(item)
        for match in PROCESS_ENV_RE.finditer(content):
            key = match.group(1) or match.group(2)
            item = EnvEvidence(
                key=key,
                required=False,
                secret=is_secret_key(key),
                sources={f"code:{relative}:process_env"},
            )
            env.setdefault(key, EnvEvidence(key=key)).merge(item)
        for match in SCHEMA_KEY_RE.finditer(content):
            key, library = match.group(1), match.group(2)
            line_end = content.find("\n", match.start())
            fragment = content[match.start():line_end if line_end >= 0 else match.end() + 120]
            default_match = re.search(r"\.default\(\s*([^\)]+)\)", fragment)
            default = _literal_default(default_match.group(1)) if default_match else None
            item = EnvEvidence(
                key=key,
                value=default,
                required=(
                    default is None
                    and (".required(" in fragment
                    or (library == "z" and ".optional(" not in fragment)
                    )
                ),
                secret=is_secret_key(key),
                sources={f"code:{relative}:schema"},
                has_code_default=default is not None,
            )
            env.setdefault(key, EnvEvidence(key=key)).merge(item)
        for match in STRING_RE.finditer(content):
            value = match.group(2)
            if TOPIC_RE.fullmatch(value):
                topics.add(value)
    return env, topics, realm_access, resource_access


def node_major_from_engine(engine: Optional[str]) -> int:
    if not engine:
        return DEFAULT_NODE_MAJOR
    candidates: List[int] = []
    for branch in engine.split("||"):
        exact = re.search(r"(?:^|\s)[~^=]?\s*v?(\d+)(?:\.x|\.\d+|\s|$)", branch)
        lower = re.search(r">(=)?\s*v?(\d+)", branch)
        if lower:
            major = int(lower.group(2)) + (0 if lower.group(1) else 1)
            candidates.append(major)
        elif exact:
            candidates.append(int(exact.group(1)))
        else:
            upper = re.search(r"<(=)?\s*v?(\d+)", branch)
            if upper:
                limit = int(upper.group(2)) + (1 if upper.group(1) else 0)
                if DEFAULT_NODE_MAJOR < limit:
                    candidates.append(DEFAULT_NODE_MAJOR)
    return min(candidates) if candidates else DEFAULT_NODE_MAJOR


def parse_prisma_schemas(service_path: Path) -> List[str]:
    schema = service_path / "prisma" / "schema.prisma"
    if not schema.is_file():
        return []
    content = re.sub(r"//[^\n]*", "", schema.read_text(errors="replace"))
    schemas: Set[str] = set()
    for datasource in re.finditer(r"\bdatasource\s+\w+\s*\{(.*?)\}", content, re.DOTALL):
        match = re.search(r"\bschemas\s*=\s*\[(.*?)\]", datasource.group(1), re.DOTALL)
        if match:
            schemas.update(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))
    return sorted(schemas)


def parse_prisma_env(service_path: Path) -> Dict[str, EnvEvidence]:
    schema = service_path / "prisma" / "schema.prisma"
    if not schema.is_file():
        return {}
    content = re.sub(r"//[^\n]*", "", schema.read_text(errors="replace"))
    result: Dict[str, EnvEvidence] = {}
    for key in re.findall(r"\benv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\)", content):
        result[key] = EnvEvidence(
            key=key,
            required=True,
            secret=is_secret_key(key),
            sources={"prisma:schema.prisma:env"},
        )
    return result


def detect_health_paths(service_path: Path) -> List[str]:
    candidates: Set[str] = set()
    for source in iter_production_sources(service_path):
        content = _strip_js_comments(source.read_text(errors="replace"))
        for path in re.findall(r"['\"](/health(?:/[A-Za-z0-9_-]+)?)['\"]", content):
            candidates.add(path)
        controllers = re.findall(r"@Controller\(\s*['\"]([^'\"]*)['\"]\s*\)", content)
        if any(value.strip("/") == "health" for value in controllers):
            routes = re.findall(r"@Get\(\s*['\"]?([^'\"\)]*)['\"]?\s*\)", content)
            if not routes:
                candidates.add("/health")
            for route in routes:
                suffix = route.strip().strip("/")
                candidates.add("/health" + (f"/{suffix}" if suffix else ""))
    order = {"/health/ready": 0, "/health/live": 1, "/health": 2}
    return sorted(candidates, key=lambda path: (order.get(path, 10), path))


def audit_service(service_path: Path, name: Optional[str] = None) -> ServiceAudit:
    package_path = service_path / "package.json"
    package: Dict[str, Any] = {}
    if package_path.is_file():
        try:
            parsed = json.loads(package_path.read_text())
            if isinstance(parsed, dict):
                package = parsed
        except (OSError, json.JSONDecodeError):
            pass
    env_example, _ = parse_env_example(service_path / ".env.example")
    code_env, source_topics, realm_access, resource_access = scan_code_contract(service_path)
    merged: Dict[str, EnvEvidence] = {}
    prisma_env = parse_prisma_env(service_path)
    for collection in (code_env, prisma_env, env_example):
        for key, item in collection.items():
            merged.setdefault(key, EnvEvidence(key=key)).merge(item)

    deps: Dict[str, Any] = {}
    for field_name in ("dependencies", "devDependencies"):
        value = package.get(field_name)
        if isinstance(value, dict):
            deps.update(value)
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    engine = None
    if isinstance(package.get("engines"), dict):
        raw_engine = package["engines"].get("node")
        engine = raw_engine if isinstance(raw_engine, str) else None
    start_script = "prod" if "prod" in scripts else "start:prod" if "start:prod" in scripts else None
    topics = set(source_topics)
    for key, item in merged.items():
        if "TOPIC" in key and item.value and TOPIC_RE.fullmatch(item.value):
            topics.add(item.value)

    return ServiceAudit(
        name=name or service_path.name,
        env=merged,
        env_example_keys=set(env_example),
        code_env_keys=set(code_env),
        node_major=node_major_from_engine(engine),
        node_engine=engine,
        start_script=start_script,
        build_script="build" if "build" in scripts else None,
        prisma="prisma" in deps or "@prisma/client" in deps,
        prisma_schemas=parse_prisma_schemas(service_path),
        health_candidates=detect_health_paths(service_path),
        topics=sorted(topics),
        realm_access_claim=realm_access,
        resource_access_claim=resource_access,
    )
