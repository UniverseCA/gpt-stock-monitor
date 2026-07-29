# Contributing

感谢参与。改动应小而清晰，并保持安全边界可验证。

## 环境搭建

CI 使用 Python 3.12。PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

项目元数据允许 Python 3.11 及以上版本，但提交前请至少在 Python 3.12 上验证，以匹配 GitHub Actions。

## 质量检查

提交前依次运行：

```powershell
ruff format --check .
ruff check .
mypy src
python -m pytest -q
```

若你主动格式化代码，使用 `ruff format .`，然后重新运行以上检查。

## 开发流程

采用 TDD：

1. 先写能复现缺陷或定义新行为的测试，并确认测试失败。
2. 写解决问题所需的最小实现。
3. 运行相关 targeted 测试，再运行完整测试和静态检查。
4. 检查 `git diff --check`，按单一目的创建小提交。

只修改任务需要的文件；不要顺带重构或格式化无关代码。

## 测试与安全

- 测试必须禁真实网络：使用 `tests/fixtures`、fake adapter、临时 Git 仓库和 HTTP mock。
- CI 会在依赖和 Chromium 安装后启用操作系统级 IPv4/IPv6 出站保护，只允许 loopback 和已有连接，再以 `no-new-privs` 运行 pytest。
- 本地测试必须保持离线；Playwright 用例应通过 `page.route` 提供样本。Python fixture 或 mock 本身不能单独阻止测试启动的子进程访问网络。
- 禁止在测试、提交、日志、Issue 或截图中使用真实 Secret、Cookie、token 或飞书 Webhook。
- 示例只能使用变量名或明显占位符，不要使用形似真实 token 的值。
- 浏览器采集必须保持普通 Playwright 行为；不得增加隐身、指纹伪装、验证码破解或绕过逻辑。
- 遇到验证码或交互式验证应安全失败，不得绕验证码。
- 任何真实店铺访问或真实通知都需要仓库所有者明确授权，不属于自动测试。

提交 Pull Request 时，请说明 RED 测试、GREEN 验证、行为变化和未执行的真实环境验证。
