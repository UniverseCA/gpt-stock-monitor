# Open-source release checklist

本清单用于准备本地发布候选，不授予推送、创建仓库、Release 或发送消息的权限。

## 元数据与文档

- [ ] 确认项目名称、简介、支持范围与仓库 URL。
- [ ] 替换版权名称：将 `LICENSE` 和 README 中的 `[Project Maintainers]` 改为经确认的真实版权名称；未替换前不得发布。
- [ ] 确认 MIT License 适用于全部拟发布代码和依赖组合。
- [ ] 复核 README 的 10 分钟路径、限制、合规说明和“不保证购买成功”。
- [ ] 确认 CONTRIBUTING、SECURITY 与实际工作流一致。
- [ ] 确认 GitHub **Private vulnerability reporting** 已启用；若未启用，维护者应先确定并公开一个私密报告渠道。

## Secret 与安全

- [ ] 扫描仓库，确认不存在真实飞书 hook、token、Cookie、凭据或本地环境文件。
- [ ] 确认 `FEISHU_WEBHOOK_URL` 只作为 `monitor-production` Environment secret 配置，不记录其值。
- [ ] 确认店铺 URL、最终重定向、浏览器挑战和 Secret 脱敏测试通过。
- [ ] 确认测试禁真实网络，不访问真实店铺、不调用真实 Webhook。
- [ ] 确认没有验证码绕过、代理池、自动购买或下单能力。

## 代码质量与构建

- [ ] 使用 Python 3.12 创建干净环境并安装 `.[dev]`。
- [ ] 运行 `python -m playwright install chromium`。
- [ ] 运行 `ruff format --check .`。
- [ ] 运行 `ruff check .`。
- [ ] 运行 `mypy src`。
- [ ] 使用全新临时 basetemp 运行完整测试和覆盖率；若 Windows ACL 阻止，保留错误记录，不删除或修改锁定目录。

PowerShell：

```powershell
$pytestDir = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), 'gpt-stock-monitor-pytest-' + [guid]::NewGuid())
python -m pytest --basetemp $pytestDir --cov=gpt_stock_monitor --cov-report=term-missing -q
```

Bash：

```bash
pytest_dir="$(mktemp -d)"
python -m pytest --basetemp "$pytest_dir" --cov=gpt_stock_monitor --cov-report=term-missing -q
```

- [ ] 运行与配置、CLI、工作流、URL/重定向、Secret 和状态 CAS 相关的 targeted 测试。
- [ ] 确认 CI 在依赖和 Chromium 安装后以 IPv4/IPv6 出站规则、loopback/已有连接例外及 `no-new-privs` 运行 pytest，并可靠清理本步骤规则。
- [ ] 运行 `git diff --check`。
- [ ] 运行 `python -m build`，检查 sdist 和 wheel。
- [ ] 在干净临时虚拟环境中安装 wheel，并运行 `python -m gpt_stock_monitor.cli --help`。

## GitHub Fork 配置

- [ ] Fork 中已提交正确的 `config/monitors.yaml`。
- [ ] 已创建 `monitor-production` Environment，只允许 Fork 默认分支部署，且未配置阻塞定时任务的逐次人工批准。
- [ ] 已创建飞书自定义机器人，Webhook 仅存入该 Environment 的 `FEISHU_WEBHOOK_URL` secret。
- [ ] **Settings → Actions → General → Workflow permissions** 已设为 **Read and write permissions**。
- [ ] 手动 `dry_run=true` 已成功；明确它会访问真实店铺，但不会发送消息或写状态。
- [ ] 首次授权的实时运行已建立静默基线。
- [ ] `monitor-state` 分支存在，`state.json` 可读取，CAS 更新行为正常。
- [ ] 已记录 cron 可能延迟，未承诺精确五分钟执行。

## 真实 Fork 冒烟记录与授权门

真实 Fork 冒烟只能在用户明确授权且提供 GitHub 仓库后进行。授权前：

- 不推送任何提交；
- 不创建远端仓库、Pull Request 或 Release；
- 不运行会访问真实店铺的 CLI/工作流；
- 不发送真实飞书消息。

获得授权后，在发布记录中填写以下内容，但绝不记录 webhook：

```text
日期：
GitHub 仓库：
workflow URL：
dry-run 结果：
首次静默基线结果：
monitor-state 状态分支提交：
通知/CAS 验证结果：
执行人：
```

## 最终发布

- [ ] `git status` 只包含预期文件，提交历史不含 Secret。
- [ ] 发布提交和标签指向已经验证的同一源代码状态。
- [ ] 版权名称已替换并由维护者确认。
- [ ] 用户已明确授权推送及创建 Release。
- [ ] 发布说明准确列出首版范围与已知限制。
