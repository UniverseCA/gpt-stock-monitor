# Security Policy

## 报告漏洞

请优先使用仓库 **Security** 页面中的 GitHub **Private vulnerability reporting** 私密报告漏洞。若项目尚未启用该功能，请通过维护者已经公开指定的私密渠道联系维护者；不要猜测或使用本文未列出的邮箱。

不要在公开 Issue、Discussion、Pull Request、Actions 日志或截图中粘贴飞书 Webhook、token、Cookie、凭据或可复现的敏感利用细节。公开 Issue 只应说明“已通过私密渠道报告”，不应包含 Secret。

报告中可包含：

- 受影响版本或提交；
- 最小复现步骤和影响；
- 已脱敏的日志；
- 建议修复方向。

维护者确认可以公开前，请保持漏洞细节私密。

## 安全边界

- **店铺 URL：** 仅允许 HTTPS `pay.ldxp.cn` 的 `/shop/<shop_id>` 路径、默认 HTTPS 端口，禁止查询参数、片段和用户凭据。
- **重定向：** 浏览器导航后的最终 URL 必须仍是获准主机、路径和相同 `shop_id`；跨店铺或跨主机重定向会失败。
- **Webhook Secret：** 只通过仓库 Secret `FEISHU_WEBHOOK_URL` 注入实时通知步骤，不应进入配置、源码、状态分支或日志。CLI 对已知 Webhook/token 和异常中的相同模式进行脱敏；这不是公开 Secret 的许可。
- **浏览器：** 使用普通无头 Playwright Chromium，不使用代理池、隐身或反检测技术。检测到验证码或交互式挑战时安全失败，不尝试绕过。
- **通知：** 状态先持久化、通知后确认，提供至少一次投递语义；故障窗口内可能重复发送。事件 ID 可用于去重。

轮换任何疑似泄露的 Webhook，并检查仓库历史、Actions 日志和飞书群消息。
