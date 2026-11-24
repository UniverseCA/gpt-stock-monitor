# 重启：迁移到 wzyp.cn 并改为自托管运行

**日期：** 2026-11-24

**状态：** 用户已拍板，进入实现

## 1. 背景

链动小铺（ldxp）店铺页已从 `pay.ldxp.cn` 迁到 `wzyp.cn`，`pay.ldxp.cn` 现在直接重置连接。
用户从国内网络打开 `https://wzyp.cn/shop/Aileader` 不再出现验证；但从海外机房（云端会话、
GitHub 托管 runner）访问，仍然返回阿里云 ESA 的滑块验证页（"Please slide to verify"）。

项目继续坚持：不绕过验证码、不用代理池或指纹伪装。

## 2. 已拍板的决定

| 事项 | 决定 |
|---|---|
| 运行位置 | 用户自己的电脑（Windows），作为 GitHub 自托管 runner |
| 监控对象 | `https://wzyp.cn/shop/Aileader` 的 `G成品号` 分类，配置保持可扩展到多个分类 |
| 通知 | 继续用飞书 Webhook |
| 频率 | 继续 `*/5 * * * *`，电脑关机或 runner 未运行时自然跳过 |

## 3. 现场核对结果

用户保存的真实页面 HTML 与现有适配器依赖的 DOM 一致：`.fl_box_leng` / `.fl_box_leng_xz`、
`共N种商品`、`.goods_item.has_image`、`.name`、`.goods-price .currency` / `.nowPrice`、`.stock`。

同时发现原实现从未在真实页面跑通的两个缺陷：

1. 页面静态引用 `https://push.zhanzhang.baidu.com/push.js`。原请求守卫把任何站外子资源都标记为
   “不安全”并让整次采集失败，因此在真实页面上每次都会失败。
2. ESA 验证页是英文/中文双语文案（"verify that you are a real person"、"验证您是真人"、
   "Please slide to verify"），原识别只匹配「安全验证 / 人机验证」，会被误判为“找不到分类”。

另有新库存文案 `库存一般`（class `rank30`），原映射会落到 `unknown`。

## 4. 改动与接口

### 4.1 站点与配置

- `config.py`：店铺主机从 `pay.ldxp.cn` 改为 `wzyp.cn`，其余 URL 规则不变
  （HTTPS、默认端口、无凭据/查询/片段、路径精确为 `/shop/<shop_id>`）。主机名集中为常量 `SHOP_HOST`。
- `sites/ldxp.py`：
  - 子资源与商品链接使用 `SHOP_HOST`，商品 URL 为 `https://wzyp.cn/item/<id>`；
  - **请求守卫调整**：未批准的导航（含重定向、弹窗）仍然中止并使采集失败；未批准的**子资源**
    只中止、不发出网络请求，不再让整次采集失败；
  - 验证页识别扩展为中英文 ESA 文案；
  - `库存一般` 映射为 `in_stock`。
- `monitor.py`：站点异常时向 stderr 输出一行诊断（监控 ID、异常类型、适配器内的固定文案），
  便于在用户电脑上排查；不改变状态和事件 ID 的计算。
- `config/monitors.yaml`：改为 `Aileader` / `G成品号`。

### 4.2 工作流

- `monitor.yml` 的 job 改为 `runs-on: [self-hosted, stock-monitor]`。
- 不再使用 `actions/setup-python`（它在 Windows 自托管 runner 上需要管理员权限和 7zip，并修改注册表）；
  改为使用电脑上预装的 Python 3.12。
- Chromium 安装改为 `python -m playwright install chromium`（`--with-deps` 只适用于 Linux 系统依赖）。
- `ci.yml` 不变：仍在 GitHub 托管的 Ubuntu 上离线跑测试，不访问真实店铺。

## 5. 验收标准

离线（本仓库 CI 与本地 `pytest`）：

1. `ruff format --check .`、`ruff check .`、`mypy src`、`pytest -q` 全部通过。
2. 配置只接受 `https://wzyp.cn/shop/<id>`，拒绝 `pay.ldxp.cn` 与其他主机。
3. 页面引用站外脚本时：该请求不会到达网络，采集照常成功。
4. 未批准的导航、重定向、弹窗：仍然中止并以 `SiteNavigationError` 失败。
5. 英文和中文 ESA 验证页都识别为 `InteractiveChallengeError`，且不重试。
6. `库存一般` → `in_stock`。
7. 工作流契约测试覆盖：自托管标签、无 `setup-python`、安装命令、密钥作用域不变。

在线（用户电脑，需要用户执行）：

8. 手动 `--dry-run` 在 `Aileader` / `G成品号` 上成功输出商品列表，且每个商品都拿到 `/item/<id>`
   稳定 ID（点击商品后的弹窗结构此前无法离线核对，这一条是首要验证点）。
9. 自托管 runner 上 `Monitor stock` 的 dry-run 成功；随后非 dry-run 静默建立基线。

## 6. 风险

- 弹窗结构（`.arco-modal.confirm_order`、`.pr_name`、`a[href^="/item/"]`）未经新页面核对；
  若不符，第 8 条会以 `SuspiciousExtractionError` 失败，并由第 4.1 节的诊断输出指出具体环节。
- 电脑关机、睡眠或断网期间不监控；GitHub 不会为离线的自托管 runner 补跑。
- 若国内网络也开始出现滑块，监控会以验证失败告警，不会尝试绕过。
