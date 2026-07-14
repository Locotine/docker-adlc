# Docker Claude Toolkit

Marketplace/plugin cho Claude Code, dùng để bổ sung bộ Docker infrastructure skills và scripts vào nhiều project mà không thay thế thư mục `.claude` hoặc `scripts` đã có.

## Cơ chế cài đặt

Claude Code luôn cài marketplace plugin vào cache riêng, không có lifecycle hook chạy đúng tại thời điểm `/plugin install` để tự chép file vào project. Vì vậy quy trình an toàn gồm hai lệnh:

1. Cài plugin từ marketplace.
2. Chạy skill `/docker-claude:install-project` để merge payload vào project hiện tại.

Installer xử lý từng file:

- Tạo `.claude/skills` và `scripts` nếu chưa có.
- Chỉ copy file chưa tồn tại.
- File đã có và giống nhau được giữ nguyên, báo `unchanged`.
- File đã có nhưng khác nội dung được giữ nguyên, báo `conflict`.
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

Với `user` scope, plugin có thể được gọi bởi user hiện tại trong nhiều project. Dù chọn scope nào, payload `.claude/skills` và `scripts` chỉ được merge vào project khi bạn chủ động chạy `install-project` tại project đó.

## Preview trước khi merge

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

Việc chạy lại installer chỉ thêm file mới. File project đã tồn tại không bị update âm thầm; conflict sẽ được liệt kê để xử lý thủ công.

Từ phiên bản `1.0.1`, skill `bootstrap` được đổi tên thành `docker-bootstrap`. Với project đã cài bản cũ, chạy lại `/docker-claude:install-project` để thêm skill mới. Installer giữ nguyên `.claude/skills/bootstrap` cũ theo nguyên tắc không tự xóa file project; có thể xóa skill cũ thủ công sau khi xác nhận project đã nhận `.claude/skills/docker-bootstrap`.

## Cấu trúc

```text
.claude-plugin/
  marketplace.json       # catalog của driverplus-tools
  plugin.json            # manifest của docker-claude
.claude/skills/           # payload skills được merge vào project
skills/install-project/   # skill namespaced dùng để chạy installer
scripts/                  # payload scripts + installer (installer không được copy)
tests/                    # test đảm bảo merge không phá file hiện có
```
