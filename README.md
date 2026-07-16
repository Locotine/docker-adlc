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

`docker-bootstrap` sẽ lần lượt khởi tạo `infra/` nếu chưa có, kiểm tra `infra/.env`, bật infrastructure và application services, verify environment rồi in các URL localhost. Lệnh không tự xóa container, image hoặc volume.

Ở chế độ `--yes`, các giá trị `REPLACE_ME_*` trong `.env.example` được thay bằng secret local ngẫu nhiên trước khi Docker khởi động; giá trị secret không được in ra output. Nếu project có Python/Go service chưa có Dockerfile, preflight sẽ yêu cầu quyết định thêm Dockerfile hoặc loại service khỏi plan thay vì tạo compose không build được.

Khi gọi qua Claude Code, skill sẽ tự scan project trước. Các lựa chọn chắc chắn từ source được áp dụng tự động; các điểm mơ hồ như thiếu/trùng port, Dockerfile còn thiếu hoặc dependency chỉ gợi ý module sẽ được gom lại hỏi một lượt theo kiểu Grill Me. Sau khi nhận câu trả lời, agent tự chạy tiếp đến cuối — user không phải copy lệnh sang terminal.

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

`bootstrap.sh` không tự sinh secret thật. Không commit `infra/.env` hoặc các file chứa credential.

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

Ba boundary được `sync-env-docker` hỗ trợ hiện tại là `d-bff-auth-client`, `d-identity-trust` và `d-taxonomy`.

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
