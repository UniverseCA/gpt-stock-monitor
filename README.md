# GPT Stock Monitor

用 GitHub Actions 定时打开指定的 LDXP 店铺页面，比较商品名称、价格、库存文案和可用状态，并把变化发送到飞书群。状态保存在独立的 `monitor-state` Git 分支中，不需要数据库或常驻服务器。

> [!IMPORTANT]
> `LICENSE` 仍使用版权人占位符 `[Project Maintainers]`。公开发布前必须确认并替换为真实版权名称。

## 10 分钟开始使用

### 1. Fork 仓库

在 GitHub 页面选择 **Fork**，然后进入你自己的 Fork。工作流需要向这个 Fork 的 `monitor-state` 分支写入状态。

进入 **Actions** 页面确认或启用工作流。Fork 中工作流可能默认禁用：若页面显示禁用提示，阅读提示后选择启用；若工作流已经可见，则只需确认 **CI** 和 **Monitor stock** 均在列表中。完成此步后再继续配置 Actions 写权限和运行工作流。

### 2. 配置监控项

直接在 Fork 中编辑并提交已纳入版本控制的 [`config/monitors.yaml`](config/monitors.yaml)：

```yaml
monitors:
  - id: my-shop
    name: 我的店铺
    url: https://pay.ldxp.cn/shop/SHOP_ID
    categories:
      - 目标分类名称
```

- `id` 在所有监控项中必须唯一。
- `name`、`id` 和 `categories` 不能为空。
- URL 只接受 `https://pay.ldxp.cn/shop/<shop_id>`（可显式使用 443 端口），不接受查询参数、片段、用户凭据或其他主机。
- 页面重定向后仍必须是同一 `shop_id`；否则本次采集失败。

### 3. 创建飞书自定义机器人

在接收通知的飞书群中进入 **设置 → 群机器人**，添加自定义机器人并复制 Webhook。飞书的[自定义机器人使用指南](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)说明了当前操作入口和安全设置。

Webhook 等同于密码：不要写进 YAML、代码、提交、Issue、截图或日志。

### 4. 创建受保护 Environment 并添加 Secret

在 Fork 中打开 **Settings → Environments → New environment**，创建固定名称 `monitor-production`。在该 Environment 的 **Deployment branches and tags** 中只允许 Fork 的默认分支部署；不要配置每次运行都需要人工批准的保护规则，否则每 5 分钟的定时任务无法自动执行。

随后在 `monitor-production` 的 **Environment secrets → Add secret** 中添加：

- Name：`FEISHU_WEBHOOK_URL`
- Secret：上一步复制的完整 Webhook

该值必须是 Environment secret，不是 repository secret。程序只接受 HTTPS、主机为 `open.feishu.cn`、标准机器人路径且无查询参数/片段/凭据的 Webhook。CLI 会对异常中的 Webhook 和 token 做脱敏，但仍应避免主动打印 Secret。

### 5. 允许 Actions 写状态分支

打开 **Settings → Actions → General → Workflow permissions**，选择 **Read and write permissions** 并保存。仓库内监控工作流也显式声明了 `contents: write`；缺少仓库级写权限时无法创建或更新 `monitor-state`。

### 6. 手动 dry-run

进入 **Actions → Monitor stock → Run workflow**，保持 `dry_run` 为 `true` 后运行。

dry-run 会使用普通无头 Chromium 访问配置的真实店铺并读取远端状态，但不会发送飞书消息，也不会写入 `monitor-state`。因此它不是禁网测试；只有在你有权访问目标页面并接受这次真实请求时才运行。

### 7. 建立首次静默基线

确认 dry-run 成功后，再手动运行一次并将 `dry_run` 设为 `false`。首次**成功**采集没有旧快照可比较，会静默建立基线并创建 `monitor-state`，通常不会发送库存变化通知。若首次采集本身失败，健康事件可能触发告警，不能视为静默基线。

### 8. 查看状态与后续运行

在仓库分支选择器中查看 `monitor-state` 分支及其 `state.json`。不要手工编辑该分支；运行器通过精确 lease 的 CAS（compare-and-swap）更新它，避免并发运行静默覆盖状态。

工作流配置为每 5 分钟一次的 cron。GitHub Actions 的定时任务在高负载时可能延迟，频率不是实时保证。后续成功采集与已保存快照不同才会形成变化通知。

## 行为和安全边界

- 使用普通 Playwright Chromium 页面，不使用隐身、指纹伪装、代理池或其他规避手段。
- 检测到安全验证或人机验证码时立即失败；项目不会绕验证码。
- 配置 URL 与最终重定向 URL 都经过严格校验。
- Webhook 只从 `FEISHU_WEBHOOK_URL` 环境变量读取，错误输出会脱敏。
- 通知采用至少一次语义：事件先持久化到状态分支，再发送并确认。若消息已送达、确认提交前进程中断，重试可能产生重复通知；接收方可用消息中的事件 ID 去重。
- CAS 保护状态更新，但不承诺绝不重复通知，也不保证定时任务准点。

首版明确不含 Web 后台、数据库、多渠道通知、代理池、自动购买或下单功能。

## 故障排查

### 工作流无法写入 `monitor-state`

确认 Fork 的 **Workflow permissions** 为 **Read and write permissions**，工作流不是从只读 Pull Request 上下文运行，并查看 Actions 日志中的通用错误。不要把 Webhook 粘贴到日志或 Issue。

### 提示 `configuration error`

检查 YAML 缩进、必填字段、重复 `id`，并确认店铺 URL 精确符合 `https://pay.ldxp.cn/shop/<shop_id>`。非 dry-run 还必须在 `monitor-production` Environment 中配置有效的 `FEISHU_WEBHOOK_URL`。

### 提示 `monitor run failed` 或出现健康告警

可能原因包括网络失败、页面结构变化、分类不存在、可疑空结果、重定向到未批准 URL，或出现验证码。项目会保守失败，不会尝试绕过交互式验证。先在获得目标站点访问授权的前提下运行手动 dry-run，并检查脱敏日志。

### 没有立即收到通知

首次成功运行只建立基线；无变化时也不会发送库存变化通知。检查 `monitor-state` 是否已创建、Secret 名称是否正确，以及计划任务是否延迟。飞书发送成功但确认状态失败时，下一次可能重发同一事件。

## 本地开发

推荐使用与 CI 一致的 Python 3.12：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
ruff format --check .
ruff check .
mypy src
python -m pytest -q
```

CI 在依赖和 Chromium 安装完成后用操作系统级 IPv4/IPv6 出站规则运行 pytest，只保留 loopback 和已有连接，并以 `no-new-privs` 防止测试子进程提权撤销规则。本地测试也必须保持离线：使用现有 HTML fixture、fake adapter、HTTP mock，并通过 Playwright `page.route` 提供样本，不访问真实店铺或真实飞书 Webhook。Python fixture 或 mock 本身不能单独保证所有子进程断网。

查看 CLI：

```powershell
python -m gpt_stock_monitor.cli --help
```

本地执行 `python -m gpt_stock_monitor.cli --config config/monitors.yaml --dry-run` 仍会读取 Git 远端并访问配置中的真实店铺；不要把它当作离线验证命令。

## 架构

一次 CLI 运行按以下边界执行：

1. `config.py` 读取并严格验证 YAML 和店铺 URL。
2. `sites/ldxp.py` 用普通 Chromium 采集并规范化分类商品。
3. `monitor.py` 比较快照、维护健康状态和待发送事件。
4. `state_git.py` 在独立 `monitor-state` 分支用 CAS 发布 `state.json`。
5. `notifiers/feishu.py` 构造、分片并发送飞书消息；发送确认后清除待发送事件。

`--dry-run` 在比较后返回摘要，不发送通知，也不发布新状态。

## 合规与免责声明

仅监控你有权访问的公开或获授权页面，并遵守目标网站条款、robots 政策、频率限制及适用法律。不要用本项目绕过验证码、访问控制或反自动化措施。

库存信息可能延迟、误判或在下单前变化。本项目只提供尽力而为的变化提醒，不构成购买承诺，也不保证购买成功。使用者自行承担访问、通知和交易决策的责任。
