# Docker Claude Toolkit

Marketplace/plugin cho Claude Code, dùng để đồng bộ bộ Docker infrastructure skills và scripts vào nhiều project mà không thay thế toàn bộ thư mục `.claude` hoặc `scripts` đã có.

## Cơ chế cài đặt

Claude Code luôn cài marketplace plugin vào cache riêng, không có lifecycle hook chạy đúng tại thời điểm `/plugin install` để tự chép file vào project. Vì vậy quy trình an toàn gồm hai lệnh:

1. Cài plugin từ marketplace.
2. Chạy skill `/docker-claude:install-project` để đồng bộ payload vào project hiện tại.

Installer xử lý từng file:

- Tạo `.claude/skills` và `scripts` nếu chưa có.
- Copy file plugin chưa tồn tại.
- File plugin đã có và giống nhau được giữ nguyên, báo `unchanged`.
- File plugin đã có nhưng khác nội dung được ghi đè nguyên tử, báo `overwritten`.
- File khác không thuộc payload plugin được giữ nguyên.
- Không xóa hoặc thay thế nguyên folder.
- Không publish/copy `.claude/settings.local.json`.

## Cài từ GitHub

Claude Code hỗ trợ ba scope khi cài plugin:

| Scope | Phạm vi |
| --- | --- |
| `user` | Dùng cho user hiện tại trong mọi project; đây là mặc định và là lựa chọn gần nhất với "global". |
| `project` | Dùng chung cho repository qua `.claude/settings.json`. |
| `local` | Chỉ dùng cho user hiện tại trong repository này qua `.claude/settings.local.json`. |

CLI không có scope `global` hoặc system-wide cho tất cả user trên máy. Để chọn scope ngay lúc cài, chạy đoạn sau trong terminal:

```bash
read -r -p "Scope [user/project/local] (user): " SCOPE
SCOPE="${SCOPE:-user}"
case "$SCOPE" in
  user|project|local) ;;
  *) echo "Scope không hợp lệ: $SCOPE" >&2; exit 2 ;;
esac

claude plugin marketplace add Locotine/docker-adlc --scope "$SCOPE"
claude plugin install docker-claude@driverplus-tools --scope "$SCOPE"
```

Hoặc thêm marketplace, mở `/plugin`, vào tab **Discover**, chọn `docker-claude`, rồi chọn `User`, `Project` hoặc `Local` trong giao diện cài đặt. Sau đó mở Claude Code tại project đích và chạy:

```text
/docker-claude:install-project
/reload-plugins
```

Với `user` scope, plugin có thể được gọi bởi user hiện tại trong nhiều project. Dù chọn scope nào, payload `.claude/skills` và `scripts` chỉ được đồng bộ vào project khi bạn chủ động chạy `install-project` tại project đó.

## Update project đã cài plugin

Sau khi producer publish version mới, chạy trong từng project consumer:

```text
/plugin marketplace update driverplus-tools
/plugin update docker-claude@driverplus-tools
/reload-plugins
/docker-claude:install-project
/reload-plugins
```

Lần `/reload-plugins` đầu kích hoạt version plugin mới trước khi chạy installer; lần thứ hai nạp lại các project skill vừa được đồng bộ. `install-project` ghi đè toàn bộ file do plugin quản lý bằng version vừa update. Các file khác trong `.claude/skills/` và `scripts/` không thuộc payload plugin vẫn được giữ nguyên.

`install-project` cập nhật **generator/verifier/wrapper**, nhưng không tự ghi đè `infra/`
đã sinh vì thư mục đó có thể chứa thay đổi và dữ liệu cấu hình của project. Để nhận
template generator mới trong một project đã bootstrap, audit lại rồi regenerate có backup:

```bash
./scripts/infra-init.py --detect-json > /tmp/docker-plan.json
# review các uncertainties/Grill Me và lưu thành /tmp/docker-plan.reviewed.json
./scripts/infra-init.py --config /tmp/docker-plan.reviewed.json --force
# infra-init tự giữ credential cũ trong infra/.env và append key mới.
# Điền mọi REPLACE_ME_* (credential ngoài hệ thống) trước khi tiếp tục.
./scripts/bootstrap.sh --yes
```

`--force` chuyển bản cũ sang `infra.backup.<timestamp>/`, không xóa, rồi merge giá trị
`infra/.env` cũ vào contract mới để volume Postgres/Keycloak tiếp tục dùng đúng credential.
Key mới được append: `GENERATE_ME_*` là secret local mà bootstrap được phép mint;
`REPLACE_ME_*` phải lấy từ hệ thống sở hữu credential và bootstrap sẽ fail-fast nếu chưa điền.
Không commit secret. Nếu chỉ cần logic script mới mà chưa muốn regenerate Compose, dừng sau
`install-project`.

## Hướng dẫn chạy

Các lệnh bên dưới chạy trong **project đích**, sau khi đã chạy `/docker-claude:install-project` và `/reload-plugins`. Đảm bảo Docker Desktop, Colima hoặc Docker daemon đang hoạt động.

### Chạy toàn bộ dự án

Cách nhanh nhất trong Claude Code:

```text
/docker-bootstrap
```

Hoặc chạy trực tiếp trong terminal:

```bash
./scripts/bootstrap.sh --yes
```

`docker-bootstrap` sẽ audit/generate `infra/` nếu chưa có, tạo secret local, bật base
infra, reconcile PostgreSQL schema + Keycloak realm/client/role + Kafka topic, chạy
Prisma migration một lần, chờ app ready, verify strict rồi mới in URL. Lệnh không tự
xóa container, image hoặc volume; verify/provision/migration lỗi làm bootstrap fail.

Ở chế độ `--yes`, chỉ placeholder `GENERATE_ME_*` do local stack sở hữu được thay bằng
secret ngẫu nhiên trước khi Docker khởi động; giá trị không được in ra. Credential
ngoài hệ thống giữ marker `REPLACE_ME_*` và làm bootstrap dừng để user cung cấp thật.
Generator chuẩn hoá image lowercase, chọn host port trống và giữ container port ổn định. Env app được lấy
từ contract hợp nhất giữa `.env.example`, source config/validation và Prisma — không
tự bịa `REDIS_URL`, `KEYCLOAK_URL`, topic hay external service.

Khi gọi qua Claude Code, skill sẽ tự scan project trước. Các lựa chọn chắc chắn từ source được áp dụng tự động; các điểm mơ hồ như credential ngoài hệ thống, thiếu/trùng/bị chiếm port, Dockerfile còn thiếu, Dockerfile tạo UID/GID cố định, tên app trùng service infra, tên secret Postgres bị trùng sau chuẩn hoá hoặc dependency chỉ gợi ý module sẽ được gom lại hỏi một lượt theo kiểu Grill Me. Với Node service, plan có thể chọn Dockerfile sẵn có (`service`) hoặc fallback do plugin sinh (`generated`); agent không tự sửa Dockerfile của app. Sau khi nhận câu trả lời, agent tự chạy tiếp đến cuối — user không phải copy lệnh sang terminal.

Một số biến thể thường dùng:

```bash
./scripts/bootstrap.sh --build                 # build lại image trước khi chạy
./scripts/bootstrap.sh --recreate              # tạo lại container để nhận config/env mới
./scripts/bootstrap.sh --build --recreate      # build và tạo lại container
./scripts/bootstrap.sh --skip-verify           # bỏ qua bước verify environment
./scripts/bootstrap.sh --yes                   # tự chạy toàn bộ, kể cả infra-init lồng bên trong
./scripts/bootstrap.sh --init-config plan.json # chạy với lựa chọn infra-init đã review
```

Nếu `infra/.env` chưa tồn tại, có thể tạo từ template rồi điền secret thật trước khi bật toàn bộ stack:

```bash
cp infra/.env.example infra/.env
```

Các secret và realm tự sinh chỉ dùng cho local dev. Service-account realm-management
được cấp quyền rộng để local chạy được và public client có redirect wildcard; không
dùng output đó cho staging/production. Plugin không thể suy ra quyền Security, realm
ownership hay external service không có bằng chứng. Không commit `infra/.env`.

### Chạy từng tác vụ

| Mục đích | Trong Claude Code | Chạy trực tiếp trong terminal |
| --- | --- | --- |
| Khởi tạo thư mục `infra/` | `/infra-init` | `./scripts/infra-init.py` |
| Khởi tạo `infra/` tự động theo kết quả detect | `/infra-init --yes` | `./scripts/infra-init.py --yes` |
| Xem plan/điểm chưa chắc chắn dạng JSON | — | `./scripts/infra-init.py --detect-json` |
| Bật toàn bộ infra và app | `/infra-up` | `./scripts/infra-up.sh` |
| Bật lại và build image | `/infra-up --build` | `./scripts/infra-up.sh --build` |
| Chỉ bật shared infrastructure | `/infra-up --infra-only` | `./scripts/infra-up.sh --infra-only` |
| Bật tất cả application services | `/docker-apps-up` | `./scripts/docker-apps-up.sh` |
| Bật một application service | `/docker-apps-up d-taxonomy` | `./scripts/docker-apps-up.sh d-taxonomy` |
| Verify environment của boundary | `/sync-env-docker verify d-taxonomy` | `./scripts/sync-env-docker.py verify d-taxonomy` |
| Tạo `.env.local` để app local dùng Docker infra | `/sync-env-docker gen-local d-taxonomy` | `./scripts/sync-env-docker.py gen-local d-taxonomy` |
| Dừng stack nhưng giữ dữ liệu | `/infra-down` | `./scripts/infra-down.sh` |
| Chỉ dừng application services | `/infra-down --apps-only` | `./scripts/infra-down.sh --apps-only` |

`sync-env-docker` nhận mọi service trong `infra/contracts/env.json`, resolve container
qua Compose labels và verify toàn bộ replica; không còn danh sách boundary hardcode.

> **Cảnh báo:** `./scripts/infra-down.sh --volumes` xóa dữ liệu trong named volumes; `--rmi` xóa image do Compose build. Script yêu cầu xác nhận, nhưng chỉ chạy các tùy chọn này khi thực sự muốn xóa dữ liệu hoặc image.

Xem toàn bộ tùy chọn của một script bằng `--help`, ví dụ:

```bash
./scripts/bootstrap.sh --help
./scripts/infra-up.sh --help
./scripts/sync-env-docker.py verify --help
```

## Preview trước khi đồng bộ

```text
/docker-claude:install-project --dry-run
```

Cài vào project khác với project đang mở:

```text
/docker-claude:install-project --target /absolute/path/to/project
```

## Test local trước khi publish

Từ thư mục repository này:

```bash
claude plugin validate . --strict
python3 -m unittest discover -s tests -v
```

Test toàn bộ flow marketplace bằng một thư mục cấu hình Claude tạm để không ảnh hưởng cấu hình cá nhân:

```bash
export CLAUDE_CONFIG_DIR="$(mktemp -d)"
claude plugin marketplace add ./ --scope user
claude plugin install docker-claude@driverplus-tools --scope user
claude plugin list
```

Hoặc load plugin trực tiếp trong lúc phát triển:

```bash
claude --plugin-dir .
```

## Publish

1. Khởi tạo Git repository nếu cần và commit toàn bộ file, ngoại trừ `.claude/settings.local.json` đã có trong `.gitignore`.
2. Tạo GitHub repository public hoặc private rồi push branch mặc định.
3. Chạy các lệnh trong mục “Cài từ GitHub” tại từng project.
4. Khi phát hành bản mới, tăng `version` trong `.claude-plugin/plugin.json`, update marketplace rồi cài lại:

```text
/plugin marketplace update driverplus-tools
/plugin update docker-claude@driverplus-tools
/docker-claude:install-project
```

Việc chạy lại installer sẽ cập nhật mọi file thuộc payload plugin lên version mới nhất, đồng thời giữ nguyên các file khác không do plugin quản lý.

Từ phiên bản `1.0.1`, skill `bootstrap` được đổi tên thành `docker-bootstrap`. Với project đã cài bản cũ, chạy lại `/docker-claude:install-project` để thêm skill mới. Installer không xóa file không còn trong payload, nên `.claude/skills/bootstrap` cũ vẫn được giữ lại; có thể xóa skill cũ thủ công sau khi xác nhận project đã nhận `.claude/skills/docker-bootstrap`.

## Cấu trúc

```text
.claude-plugin/
  marketplace.json       # catalog của driverplus-tools
  plugin.json            # manifest của docker-claude
.claude/skills/           # payload skills được đồng bộ vào project
skills/install-project/   # skill namespaced dùng để chạy installer
scripts/                  # payload scripts + installer (installer không được copy)
tests/                    # test đảm bảo đồng bộ không đụng file ngoài payload
```
