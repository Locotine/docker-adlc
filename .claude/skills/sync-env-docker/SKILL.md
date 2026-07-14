---
name: sync-env-docker
description: Align boundary service environment against the shared Docker infra actually running. Use when the user reports "QC login fail", "env sai so với docker", "sync env với docker thực tế", "verify env container", "generate .env.local để chạy boundary local kết nối docker infra", or wants to reconcile `.env.example` schema with the values injected into a running `d-*` container. Two modes — `verify` (SELF-CHECK schema vs container, PLUS CROSS-CHECK every URL-shaped env against sibling infra containers in the same compose project: hostname alias, exposed port, postgres user/db provisioning from pg-init, Keycloak realm probe via HTTP) and `gen-local` (produce a boundary `.env.local` that talks to the running Docker infra via `localhost:<host-port>`).
---

# sync-env-docker

Wrapper skill for `scripts/sync-env-docker.py`. Two modes.

## When to use

Trigger this skill whenever:
- QC báo "không login được", "403 role", "connection refused" và ta cần biết env container có khớp với schema code không.
- User yêu cầu "sync env với docker", "generate .env.local", "verify env boundary".
- Trước khi rebuild/restart boundary — chạy `verify` để confirm không có placeholder / mismatch.

Do **not** trigger for: đọc log, restart service (dùng docker compose trực tiếp), hoặc sửa business logic.

## Usage

Boundaries known to this project: `d-bff-auth-client`, `d-identity-trust`, `d-taxonomy`.

### Mode 1 — verify (default when uncertain)

```bash
cd DRIVERPLUS-ADLC-BOUNDARIES
./scripts/sync-env-docker.py verify <boundary>
```

Output categorises problems (in order of severity):

### Schema self-check (comparing `.env.example` ↔ container)
- `MISSING` — key có trong `.env.example` nhưng không được inject vào container. Fix: thêm vào `docker-compose.apps.yml` environment.
- `PLACEHOLDER` — container env còn value dạng `REPLACE_ME*`, `your_*`. Fix: mint secret thật (Keycloak client-secret endpoint hoặc equivalent).
- `MISMATCH` — value khớp không đúng pattern trong `# expected KEY=<regex>` của `.env.example`. Ví dụ realm role `P2d` vs `P2D`.

### Infra cross-check (comparing URL-shaped env ↔ sibling infra containers in same compose project)
- `INFRA_HOST` — hostname trong URL không map về container alias nào trong project. Ví dụ `PROFILE_BASE_URL=http://d-profile:3002` khi service `d-profile` chưa deploy.
- `INFRA_USER` — Postgres user trong `DATABASE_URL` không được provisioned bởi `infra/pg-init/*.sh` và cũng không phải superuser `POSTGRES_USER`.
- `INFRA_DB` — Postgres database trong `DATABASE_URL` không có trong pg-init.
- `INFRA_REALM` — Keycloak realm trong `KEYCLOAK_ISSUER_URL` / `KEYCLOAK_JWKS_URI` không probe được HTTP 200 (realm chưa import hoặc chưa healthy).
- `INFRA_PORT_HINT` (soft, không fail) — port không nằm trong `EXPOSE`'d ports của container. Có thể container vẫn listen internal (ví dụ Redpanda `--advertise-kafka-addr redpanda:29092` chỉ advertised, không EXPOSE). Warning only.

### Info
- `EXTRA` — container có key mà `.env.example` chưa declare. Nên bổ sung schema.

Exit code non-zero nếu có bất kỳ hard problem nào (`MISSING`, `PLACEHOLDER`, `MISMATCH`, `INFRA_HOST`, `INFRA_USER`, `INFRA_DB`, `INFRA_REALM`).

Flags:
- `--skip-infra` — chỉ chạy schema self-check, bỏ qua cross-check với sibling infra. Dùng khi Docker infra chưa lên hoặc chỉ muốn nhanh.

### Mode 2 — gen-local

```bash
./scripts/sync-env-docker.py gen-local <boundary> [--out <path>]
```

Scan `docker ps` cho các infra shared (`dp-postgres`, `dp-redis`, `dp-redpanda`, `dp-keycloak`, `dp-temporal`), lấy host-port mapping thật, substitute mọi hostname `service:internal-port` trong `.env.example` → `localhost:<host-port>`. Ghi `.env.local` cho boundary chạy `npm run start` LOCAL nhưng kết nối vào Docker infra.

Default output: `<boundary>/.env.local` (đã trong `.gitignore` mặc định của template Nest).

## Behaviour rules

1. **Không auto-fix** — skill chỉ report + generate. Việc sửa `docker-compose.apps.yml` hoặc mint secret là quyết định của user (có thể là destructive).
2. **Không đọc secret** — output có thể show env key name nhưng KHÔNG print secret value (script chỉ log length).
3. **Container name mapping** ở đầu `scripts/sync-env-docker.py`. Nếu naming đổi (rebrand boundary, thêm boundary), cập nhật `BOUNDARY_CONTAINER` dict.
4. **Infra discovery cho cross-check** dùng label `com.docker.compose.project` của container app → enumerate mọi sibling container, lấy Aliases + internal Ports + env. KHÔNG hardcode `dp-*` — hoạt động với bất kỳ project name nào.
5. **Legacy `INFRA_SERVICES` list** chỉ dùng bởi `gen-local` (static mapping cho port discovery). Cross-check trong `verify` KHÔNG dùng list này.
6. **pg-init parsing** bắt được: `CREATE USER/ROLE ... PASSWORD '...'`, `CREATE DATABASE ... OWNER ...`, bash helper `create_role_and_db <role> <pass> <db>`. Thêm pattern khác thì mở rộng `parse_pg_init()`.

## Extending `.env.example` for stronger verify

Để `MISMATCH` phát hiện lỗi casing/format thay vì chỉ MISSING/PLACEHOLDER, thêm comment ngay dòng trên key:

```
# expected KEYCLOAK_REQUIRED_ROLE=^P2[A-D]$
KEYCLOAK_REQUIRED_ROLE=P2D
```

Pattern là Python regex, match toàn bộ (`re.fullmatch`).

## Related

- `infra/docker-compose.infra.yml` — shared infra (Postgres, Redis, Redpanda, Keycloak, Temporal)
- `infra/docker-compose.apps.yml` — 3 boundary apps, kết nối infra qua network `dp-boundaries`
- `infra/.env` — chứa `IDENTITY_TRUST_ADMIN_SECRET` (Keycloak client_credentials); KHÔNG commit
- `infra/keycloak-realms/dp-p2-realm.json` — nguồn truth cho role names (`P2A/B/C/D`) và client IDs
