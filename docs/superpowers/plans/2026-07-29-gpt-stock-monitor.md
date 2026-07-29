# GPT 号源监控实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个可由 GitHub Actions 每 5 分钟运行、监控 `pay.ldxp.cn` 指定商品分类并通过飞书提醒变化的开源 Python 项目。

**Architecture:** 使用 Playwright 适配器把店铺页面转换为标准化商品快照，纯函数差异层负责识别变化，监控编排层负责空分类确认和健康状态，Git 状态发布层在独立 `monitor-state` 工作树中实现至少一次通知状态机。所有线上边界都可替换，CI 使用本地 HTML 样本和模拟响应，不访问真实网站。

**Tech Stack:** Python 3.12、Playwright、Pydantic 2、PyYAML、HTTPX、pytest、pytest-httpx、Ruff、mypy、GitHub Actions

---

## 文件结构

```text
.
├─ .github/workflows/
│  ├─ ci.yml                         # 无 Secret、无真实网络的质量检查
│  └─ monitor.yml                    # 定时和手动监控工作流
├─ config/monitors.yaml              # 已提交的非敏感店铺与分类配置
├─ docs/
│  ├─ release-checklist.md           # 全新 Fork 冒烟测试记录
│  └─ superpowers/{specs,plans}/     # 设计规格和本实施计划
├─ src/gpt_stock_monitor/
│  ├─ __init__.py
│  ├─ cli.py                         # 命令入口与退出码
│  ├─ config.py                      # YAML 与 URL 严格校验
│  ├─ models.py                      # 领域模型和状态格式
│  ├─ diff.py                        # 纯语义差异比较
│  ├─ health.py                      # 异常计数、恢复和空分类确认
│  ├─ monitor.py                     # 单次运行编排
│  ├─ state.py                       # 本地状态读取、确定性序列化
│  ├─ state_git.py                   # 独立工作树和原子 Git 发布
│  ├─ notifiers/
│  │  ├─ __init__.py
│  │  └─ feishu.py                   # 消息分片、发送与成功响应校验
│  └─ sites/
│     ├─ __init__.py
│     ├─ base.py                     # 网站适配器协议和异常类型
│     └─ ldxp.py                     # pay.ldxp.cn Playwright 适配器
├─ tests/
│  ├─ conftest.py                    # 禁网夹具和通用构造器
│  ├─ fixtures/ldxp/                 # 脱敏 HTML 页面样本
│  ├─ test_models.py
│  ├─ test_sites_base.py
│  ├─ test_config.py
│  ├─ test_diff.py
│  ├─ test_feishu.py
│  ├─ test_health.py
│  ├─ test_ldxp.py
│  ├─ test_monitor.py
│  ├─ test_state.py
│  ├─ test_state_git.py
│  ├─ test_cli.py
│  ├─ test_workflows.py
│  └─ test_cli_integration.py
├─ .gitignore
├─ CONTRIBUTING.md
├─ LICENSE
├─ README.md
├─ SECURITY.md
└─ pyproject.toml
```

## 约定

- 所有任务严格按 TDD 顺序执行：先写测试并看到预期失败，再写最小实现。
- 每个任务只提交列出的文件。提交前运行针对性测试和 `git diff --check`。
- 任何测试都不得访问真实店铺或飞书；真实页面只在人工适配检查和监控工作流中访问。
- 运行命令统一使用 `python -m ...`，避免 Windows 与 Linux 的入口脚本差异。
- 实施过程中如真实 DOM 与假设不符，只修改 `sites/ldxp.py` 和对应脱敏样本，不扩散网站细节。

### 任务 1：建立 Python 项目骨架与领域模型

**文件：**
- 创建：`pyproject.toml`
- 创建：`src/gpt_stock_monitor/__init__.py`
- 创建：`src/gpt_stock_monitor/models.py`
- 创建：`tests/conftest.py`
- 创建：`tests/test_models.py`
- 创建：`.gitignore`

- [ ] **步骤 1：写领域模型失败测试**

测试 `Product` 拒绝空稳定键，`Snapshot` 拒绝重复商品键，`PendingEvent` 保存不可变消息
分片，`StateDocument` 有固定 `schema_version=1`。核心测试形态：

```python
def test_snapshot_rejects_duplicate_product_keys() -> None:
    item = Product(key="product-1", name="GPT Plus", price="13.50",
                   price_text="¥13.5", availability=Availability.IN_STOCK,
                   stock_text="有货", url="https://pay.ldxp.cn/goods/1")
    with pytest.raises(ValueError, match="duplicate product key"):
        Snapshot(monitor_id="demo", category="GPT-plus成品号",
                 products=(item, item))
```

- [ ] **步骤 2：运行测试并确认按预期失败**

运行：`python -m pytest tests/test_models.py -q`

预期：测试收集失败，提示 `gpt_stock_monitor.models` 不存在。

- [ ] **步骤 3：配置最小工程和模型**

在 `pyproject.toml` 中使用 `hatchling`，运行依赖锁定兼容范围：`pydantic>=2.8,<3`、
`PyYAML>=6,<7`、`playwright>=1.50,<2`、`httpx>=0.27,<1`；开发依赖加入 pytest、
pytest-httpx、pytest-cov、ruff、mypy、build。定义：

```python
class Availability(StrEnum):
    IN_STOCK = "in_stock"
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"

class Product(BaseModel, frozen=True): ...
class Snapshot(BaseModel, frozen=True): ...
class ChangeKind(StrEnum): ...
class Change(BaseModel, frozen=True): ...
class PendingEvent(BaseModel, frozen=True): ...
class HealthRecord(BaseModel): ...
class EmptyCandidate(BaseModel): ...
class StateDocument(BaseModel): ...
```

商品价格使用标准化十进制字符串而不是浮点数。状态键统一由
`monitor_id + "\x1f" + category` 组成，但不得出现在用户展示文本中。
`PendingEvent` 包含单调递增的 `sequence`、事件 ID 和不可变消息分片；
`StateDocument.next_event_sequence` 是下一个可分配序号，待处理队列始终按
`(sequence, event_id)` 排序，不依赖 JSON 字典顺序。

- [ ] **步骤 4：运行模型测试和静态检查**

运行：`python -m pytest tests/test_models.py -q`

运行：`python -m ruff check src tests/test_models.py`

预期：全部通过。

- [ ] **步骤 5：提交**

```powershell
git add pyproject.toml .gitignore src/gpt_stock_monitor tests/conftest.py tests/test_models.py
git commit -m "feat: add project skeleton and domain models"
```

### 任务 2：实现严格配置校验

**文件：**
- 创建：`src/gpt_stock_monitor/config.py`
- 创建：`config/monitors.yaml`
- 创建：`tests/test_config.py`

- [ ] **步骤 1：写配置失败测试**

覆盖有效示例、重复 ID、空分类、未知字段、HTTP、非默认端口、用户凭据、查询参数、片段、
非 `pay.ldxp.cn` 域名、额外路径和重定向后非法网址。对外接口：

```python
def load_config(path: Path) -> AppConfig: ...
def validate_shop_url(value: str) -> AnyHttpUrl: ...
def validate_final_url(expected_shop_id: str, value: str) -> None: ...
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_config.py -q`

预期：导入 `gpt_stock_monitor.config` 失败。

- [ ] **步骤 3：实现最小配置层**

使用 `urllib.parse.urlsplit` 做结构校验，路径正则固定为
`^/shop/(?P<shop_id>[A-Za-z0-9_-]+)$`。Pydantic 模型设置
`extra="forbid"`，并在应用级校验器中检查监控 ID 唯一。

提交的 `config/monitors.yaml` 使用示例网址和默认分类，但不含 Webhook：

```yaml
monitors:
  - id: ldxp-demo
    name: 卡密 28
    url: https://pay.ldxp.cn/shop/NIFGEAC5
    categories:
      - GPT-plus成品号
```

- [ ] **步骤 4：运行配置测试**

运行：`python -m pytest tests/test_config.py -q`

预期：全部通过，错误信息指明具体字段但不回显秘密。

- [ ] **步骤 5：提交**

```powershell
git add config/monitors.yaml src/gpt_stock_monitor/config.py tests/test_config.py
git commit -m "feat: validate monitor configuration"
```

### 任务 3：实现确定性的商品差异比较

**文件：**
- 创建：`src/gpt_stock_monitor/diff.py`
- 创建：`tests/test_diff.py`

- [ ] **步骤 1：写参数化失败测试**

分别覆盖新增、下架、补货、售罄、库存文案、价格、名称变化，以及排序、空白和等价价格
不产生变化。一个商品同时改名和改价时产生两个有固定顺序的变化。对外接口：

```python
def compare_snapshots(previous: Snapshot, current: Snapshot) -> tuple[Change, ...]: ...
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_diff.py -q`

预期：导入失败。

- [ ] **步骤 3：实现标准化和纯比较函数**

按商品键做集合比较，再按固定的 `ChangeKind` 优先级排序。名称用 Unicode NFKC、合并
空白；价格用 `Decimal` 规范化；库存原文只合并空白，不做含义猜测。不得读取时间、网络
或环境变量。

- [ ] **步骤 4：运行差异测试**

运行：`python -m pytest tests/test_diff.py -q`

预期：全部通过。

- [ ] **步骤 5：提交**

```powershell
git add src/gpt_stock_monitor/diff.py tests/test_diff.py
git commit -m "feat: detect semantic product changes"
```

### 任务 4：实现本地状态格式与确定性序列化

**文件：**
- 创建：`src/gpt_stock_monitor/state.py`
- 创建：`tests/test_state.py`

- [ ] **步骤 1：写状态读写失败测试**

覆盖不存在文件返回空状态、未知版本拒绝、损坏 JSON 拒绝、键排序、原子替换、相同状态
序列化字节完全一致、配置删除后清理对应状态，以及状态仓库协议的版本和冲突结果类型。

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_state.py -q`

预期：导入失败。

- [ ] **步骤 3：实现状态存储**

对外接口：

```python
def load_state(path: Path) -> StateDocument: ...
def serialize_state(state: StateDocument) -> bytes: ...
def write_state_atomic(path: Path, state: StateDocument) -> None: ...
def prune_unconfigured(state: StateDocument, active_keys: set[str]) -> StateDocument: ...

@dataclass(frozen=True)
class VersionedState:
    version: str | None
    document: StateDocument

class PublishStatus(StrEnum):
    PUBLISHED = "published"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"

@dataclass(frozen=True)
class PublishResult:
    status: PublishStatus
    remote_version: str | None

class StateRepository(Protocol):
    def load(self) -> VersionedState: ...
    def publish(self, expected_parent: str | None,
                state: StateDocument, message: str) -> PublishResult: ...
```

使用同目录临时文件、刷新并 `os.replace`。JSON 使用 UTF-8、`ensure_ascii=False`、排序键和
固定缩进，不写当前时间。

- [ ] **步骤 4：运行状态测试**

运行：`python -m pytest tests/test_state.py -q`

预期：全部通过。

- [ ] **步骤 5：提交**

```powershell
git add src/gpt_stock_monitor/state.py tests/test_state.py
git commit -m "feat: persist deterministic monitor state"
```

### 任务 5：实现飞书消息构造、分片和发送

**文件：**
- 创建：`src/gpt_stock_monitor/notifiers/__init__.py`
- 创建：`src/gpt_stock_monitor/notifiers/feishu.py`
- 创建：`tests/test_feishu.py`

- [ ] **步骤 1：写飞书边界失败测试**

覆盖 `code: 0`、旧版 `StatusCode: 0`、HTTP 错误、业务失败、非 JSON、超时、事件 ID、
按变化边界分片、单个项目过大时报错，以及日志和异常不包含完整 Webhook。

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_feishu.py -q`

预期：导入失败。

- [ ] **步骤 3：实现纯构造器与发送器**

```python
def build_message_parts(event_id: str, changes: Sequence[Change],
                        checked_at: datetime, max_bytes: int = 18_000) -> tuple[dict, ...]: ...

class FeishuNotifier:
    async def send_parts(self, parts: Sequence[dict]) -> None: ...
```

按序发送，每次检查 HTTP 成功和业务成功码。错误只报告响应类别和脱敏后的短摘要。不得
记录请求 URL。消息时间由编排层传入，使用 `ZoneInfo("Asia/Shanghai")`。

- [ ] **步骤 4：运行飞书测试**

运行：`python -m pytest tests/test_feishu.py -q`

预期：全部通过且 HTTP 请求完全由 `pytest-httpx` 捕获。

- [ ] **步骤 5：提交**

```powershell
git add src/gpt_stock_monitor/notifiers tests/test_feishu.py
git commit -m "feat: send chunked Feishu notifications"
```

### 任务 6：建立网站适配器协议和脱敏页面样本

**文件：**
- 创建：`src/gpt_stock_monitor/sites/__init__.py`
- 创建：`src/gpt_stock_monitor/sites/base.py`
- 创建：`tests/test_sites_base.py`
- 创建：`tests/fixtures/ldxp/in_stock.html`
- 创建：`tests/fixtures/ldxp/changed.html`
- 创建：`tests/fixtures/ldxp/explicit_zero.html`
- 创建：`tests/fixtures/ldxp/challenge.html`
- 创建：`tests/fixtures/ldxp/missing_category.html`

- [ ] **步骤 1：写适配器协议失败测试**

测试适配器结果能够区分“可信零商品”“普通商品列表”和解析异常，并定义：

```python
class SiteAdapter(Protocol):
    async def collect(self, monitor: MonitorConfig) -> Mapping[str, CategoryObservation]: ...

class SiteNavigationError(RuntimeError): ...
class InteractiveChallengeError(RuntimeError): ...
class CategoryNotFoundError(RuntimeError): ...
class SuspiciousExtractionError(RuntimeError): ...
```

- [ ] **步骤 2：运行协议测试并确认失败**

运行：`python -m pytest tests/test_sites_base.py -q`

预期：导入 `gpt_stock_monitor.sites.base` 失败。

- [ ] **步骤 3：实现最小协议层并运行测试**

在 `base.py` 定义不可变 `CategoryObservation`、`SiteAdapter` 协议和四种明确异常；
`CategoryObservation` 必须二选一地包含非空商品元组，或包含可信的
`explicit_count=0`，不得同时表示“未知空列表”。`__init__.py` 只导出公共接口。

运行：`python -m pytest tests/test_sites_base.py -q`

预期：全部通过。

- [ ] **步骤 4：进行一次人工 DOM 取样**

在本地普通 Chromium 中打开示例页，不使用隐身、代理或验证码工具。记录分类按钮、分类
计数、商品卡片、商品链接、名称、价格和库存徽标的稳定属性。复制最小必要 DOM 到样本，
删除脚本、追踪参数、Cookie、令牌和无关图片数据。若出现交互式验证码，只保存一个人工
构造的挑战页样本并停止线上取样，不尝试绕过。

- [ ] **步骤 5：检查样本不含秘密**

运行：

```powershell
rg -n "cookie|token|authorization|webhook|u_atoken|u_asig" tests/fixtures
```

预期：没有真实凭据；挑战页中如需文字匹配，只保留固定测试词。

- [ ] **步骤 6：提交协议和样本**

```powershell
git add src/gpt_stock_monitor/sites tests/test_sites_base.py tests/fixtures/ldxp
git commit -m "test: add sanitized ldxp page fixtures"
```

### 任务 7：实现 `pay.ldxp.cn` Playwright 适配器

**文件：**
- 创建：`src/gpt_stock_monitor/sites/ldxp.py`
- 创建：`tests/test_ldxp.py`

- [ ] **步骤 1：写样本驱动失败测试**

使用 Playwright `page.route()` 拦截对真实形态
`https://pay.ldxp.cn/shop/NIFGEAC5` 的请求，并用本地 HTML 样本 `fulfill`，从而在不访问
公网的情况下保留严格的 HTTPS 最终网址校验。覆盖分类定位、显式计数、商品稳定 ID、
规范链接、名称、价格、四种库存状态、重复 ID、缺少稳定 ID、挑战页、缺少分类和最终
URL 校验。不得添加跳过 URL 校验的测试开关。

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m playwright install chromium`

运行：`python -m pytest tests/test_ldxp.py -q`

预期：导入或实现断言失败；Chromium 安装成功。

- [ ] **步骤 3：实现最小适配器**

适配器只使用任务 6 确认过的选择器。导航使用显式超时、`wait_until="domcontentloaded"`，
随后等待商品区域稳定；每次导航后调用 `validate_final_url`。分类按标准化可见文字精确
匹配。将“缺货”“库存少量”等页面文案映射到枚举，无法识别时保留原文并使用
`UNKNOWN`。单个分类最多尝试 3 次，调用方负责店铺间隔。

- [ ] **步骤 4：运行适配器测试**

运行：`python -m pytest tests/test_ldxp.py -q`

预期：所有本地页面样本通过，不访问公网。

- [ ] **步骤 5：提交**

```powershell
git add src/gpt_stock_monitor/sites/ldxp.py tests/test_ldxp.py
git commit -m "feat: parse ldxp shop categories"
```

### 任务 8：实现健康状态与两次空分类确认

**文件：**
- 创建：`src/gpt_stock_monitor/health.py`
- 创建：`tests/test_health.py`

- [ ] **步骤 1：写状态机失败测试**

用表驱动序列断言失败第 1、3、12、24 次提醒，其余静默；失败后的首次成功只恢复一次；
显式零第一次只保存候选，第二次确认；中间失败会清除候选；首次空分类第二次建立静默
基线；原非空分类第二次确认时产生全部下架变化。

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_health.py -q`

预期：导入失败。

- [ ] **步骤 3：实现纯状态转换**

```python
def record_failure(state: StateDocument, key: str, reason: str) -> Transition: ...
def record_success(state: StateDocument, key: str) -> Transition: ...
def observe_explicit_zero(state: StateDocument, key: str) -> Transition: ...
```

`Transition` 同时返回新状态和需要通知的领域事件；函数不得直接发送消息或写文件。

- [ ] **步骤 4：运行状态机测试**

运行：`python -m pytest tests/test_health.py -q`

预期：全部通过。

- [ ] **步骤 5：提交**

```powershell
git add src/gpt_stock_monitor/health.py tests/test_health.py
git commit -m "feat: track monitor health and empty categories"
```

### 任务 9：实现监控编排和只读试运行

**文件：**
- 创建：`src/gpt_stock_monitor/monitor.py`
- 创建：`tests/test_monitor.py`
- 修改：`tests/conftest.py`

- [ ] **步骤 1：写端到端编排失败测试**

使用内存适配器、可脚本化 CAS 状态仓库和通知器，覆盖静默基线、无变化、变化聚合、一个
店铺失败但其他店铺继续、店铺级失败影响其全部分类，以及完整的两阶段发布循环：

- 启动时按 `(sequence, event_id)` 逐事件、逐分片处理全部旧待处理事件；
- 事件 A 全部分片成功后发布其 `delivered` 状态，再开始事件 B；
- 事件 B 中途失败时退出 3，下次只恢复 B，不能重发已确认完成的 A；
- 所有旧事件完成后重新加载远端状态，之后才开始抓取；
- 创建新待处理事件前最多进行 3 次 CAS 发布，冲突时重新加载并用已观察页面重算；
- 冲突后发现更早的待处理事件时丢弃当前观察并退出 3；
- 创建待处理事件和发送后标记送达两个阶段分别最多进行 3 次 CAS 发布；送达阶段发生
  冲突时用同一事件 ID 协调，不重新计算新事件；连续 3 次冲突后退出 3，保留可恢复事件，
  且不得创建新事件；
- 飞书失败后保留待处理事件和新快照；
- `dry-run` 不发送、不写状态，只输出差异和有序待处理摘要。

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_monitor.py -q`

预期：导入失败。

- [ ] **步骤 3：实现依赖注入式编排器**

```python
@dataclass
class RunResult:
    exit_code: int
    output: dict[str, object]

async def run_once(config: AppConfig, state_repo: StateRepository,
                   adapter: SiteAdapter, notifier: Notifier,
                   *, dry_run: bool, checked_at: datetime) -> RunResult: ...
```

事件 ID 使用 `sha256(父状态提交 + 规范化事件 JSON)`。只有非空通知内容才创建待处理
记录。`dry-run` 分支必须在任何通知或发布之前生效。编排器只依赖任务 4 的
`StateRepository` 协议；低层仓库以 `PublishResult.CONFLICT` 报告 CAS 冲突，所有阶段
重载、重算、顺序和退出规则都在此层实现并由 `test_monitor.py` 验证。

- [ ] **步骤 4：运行编排测试**

运行：`python -m pytest tests/test_monitor.py -q`

预期：全部通过。

- [ ] **步骤 5：提交**

```powershell
git add src/gpt_stock_monitor/monitor.py tests/test_monitor.py tests/conftest.py
git commit -m "feat: orchestrate one-shot monitoring"
```

### 任务 10：实现 `monitor-state` Git 工作树发布器

**文件：**
- 创建：`src/gpt_stock_monitor/state_git.py`
- 创建：`tests/test_state_git.py`

- [ ] **步骤 1：写临时 Git 仓库失败测试**

使用临时裸远端和两个克隆，覆盖孤儿分支初始化、独立工作树、无变化不提交、单提交原子
发布、固定预期父提交、禁止强推、父提交不匹配时只返回 `CONFLICT` 和最新远端版本，
在清除全局/系统 Git 身份的测试环境中仍能使用显式 bot 身份提交，以及所有命令参数不
包含 Secret。此低层测试不承担重算、待处理队列顺序或送达协调；这些属于任务 9 的
编排测试。

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_state_git.py -q`

预期：导入失败。

- [ ] **步骤 3：实现 Git 发布器**

使用 `subprocess.run([...], check=False, text=True)` 的参数列表，不拼接 Shell 字符串。
限定可调用命令为 `git fetch`、`worktree add/remove`、`switch --orphan`、`add`、`commit`、
`rev-parse`、`status --porcelain` 和普通 `push`。对外提供：

```python
class GitStateRepository(StateRepository):
    def load(self) -> VersionedState: ...
    def publish(self, expected_parent: str | None,
                state: StateDocument, message: str) -> PublishResult: ...
```

发布器只实现一次 CAS 尝试，不在内部重试；发布冲突由编排层根据阶段处理。创建的临时
工作树路径必须验证位于任务专用临时目录内，
并在 `finally` 中安全移除；不得删除仓库或用户目录。
每次 `git commit` 通过子进程环境显式设置 `github-actions[bot]` 的
`GIT_AUTHOR_NAME`、`GIT_AUTHOR_EMAIL`、`GIT_COMMITTER_NAME` 和
`GIT_COMMITTER_EMAIL`，不得依赖或修改用户的全局 Git 配置。

- [ ] **步骤 4：运行 Git 状态测试**

运行：`python -m pytest tests/test_state_git.py -q`

预期：全部通过，包括并发冲突用例。

- [ ] **步骤 5：提交**

```powershell
git add src/gpt_stock_monitor/state_git.py tests/test_state_git.py
git commit -m "feat: publish state through an isolated git branch"
```

### 任务 11：实现 CLI、退出码和全局禁网测试

**文件：**
- 创建：`src/gpt_stock_monitor/cli.py`
- 创建：`tests/test_cli.py`
- 创建：`tests/test_cli_integration.py`
- 修改：`tests/conftest.py`
- 修改：`pyproject.toml`

- [ ] **步骤 1：写 CLI 失败测试**

覆盖默认配置路径、`--config`、`--dry-run`、确定性 UTF-8 JSON 输出、缺少
`FEISHU_WEBHOOK_URL`、配置错误退出 2、运行错误退出 3、成功退出 0，以及错误输出不包含
环境变量值。

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_cli.py -q`

预期：入口不存在或断言失败。

- [ ] **步骤 3：实现 CLI**

使用标准库 `argparse`，入口为
`gpt-stock-monitor = "gpt_stock_monitor.cli:main"`。只有非试运行才要求 Webhook。捕获已
定义的应用异常并映射退出码，未知异常保留堆栈到标准错误但先经过 Secret 脱敏过滤器。
`main(argv: Sequence[str] | None = None, services_factory=build_production_services) -> int`
允许测试替换浏览器、仓库和通知边界；生产入口始终使用默认工厂，并包含
`if __name__ == "__main__": raise SystemExit(main())`。

在 `tests/conftest.py` 自动阻止未经显式标记的 socket 连接；允许的 HTTP 测试必须由
pytest-httpx 拦截。

- [ ] **步骤 4：写并运行真实 CLI 贯通测试**

`tests/test_cli_integration.py` 直接调用真实 `main(argv, services_factory=...)`，只替换外部
边界：Playwright 路由用本地 HTTPS HTML 样本响应，测试状态仓库使用临时目录中的真实
`load_state/write_state_atomic`，飞书使用 HTTPX 模拟端点。连续执行基线和变化两次，贯通
配置读取、页面解析、标准化、差异、消息分片、状态序列化和退出码，断言新增、下架、
补货、售罄、改名和改价均进入脱敏后的 JSON/飞书消息。再以待处理事件运行一次
`--dry-run`，断言磁盘字节不变且没有 HTTP 请求。

运行：`python -m pytest tests/test_cli_integration.py -q`

预期：全部通过，禁网夹具证明没有请求真实店铺或飞书。

- [ ] **步骤 5：运行全部单元和集成测试**

运行：`python -m pytest -q`

预期：全部通过，且没有真实网络访问。

- [ ] **步骤 6：提交**

```powershell
git add pyproject.toml src/gpt_stock_monitor/cli.py tests/test_cli.py tests/test_cli_integration.py tests/conftest.py
git commit -m "feat: add monitor command line interface"
```

### 任务 12：添加 CI 和定时监控工作流

**文件：**
- 创建：`.github/workflows/ci.yml`
- 创建：`.github/workflows/monitor.yml`
- 创建：`tests/test_workflows.py`

- [ ] **步骤 1：写工作流静态失败测试**

使用 `yaml.safe_load` 读取工作流，断言：Cron 为 `*/5 * * * *`；支持手动试运行；固定并发
组和 `cancel-in-progress: false`；明确超时；`monitor.yml` 仅授予
`contents: write`，`ci.yml` 仅授予 `contents: read`，其他权限不授予；PR 只运行 CI 且
不引用 Secret；监控任务安装 Chromium；状态使用独立目录；不允许 `force` 推送；监控
任务显式设置 bot author/committer 身份。

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_workflows.py -q`

预期：工作流文件不存在。

- [ ] **步骤 3：实现两个工作流**

`ci.yml` 执行 Ruff 格式检查、Ruff lint、mypy 和 pytest。依赖安装阶段只允许访问受信任
的 Python 包源和 Playwright Chromium 下载源；测试阶段由禁网夹具保证不访问真实店铺、
飞书或其他公网服务。
`monitor.yml` 使用 `actions/checkout`、Python 3.12、缓存 pip、安装 Chromium、固定
`timeout-minutes`，从 Secret 注入 Webhook，并调用一次 CLI。手动输入 `dry_run` 时不得把
Webhook 交给 CLI。所有第三方 Action 固定到已审核的完整提交 SHA，并在注释中标记版本。
工作流环境显式设置 `github-actions[bot]` 的 author/committer 名称和 noreply 邮箱，
不得执行修改全局 Git 配置的命令。

- [ ] **步骤 4：运行工作流测试与质量门禁**

运行：

```powershell
python -m pytest tests/test_workflows.py -q
python -m ruff format --check .
python -m ruff check .
python -m mypy src
python -m pytest -q
```

预期：全部通过。

- [ ] **步骤 5：提交**

```powershell
git add .github/workflows tests/test_workflows.py
git commit -m "ci: add quality and scheduled monitor workflows"
```

### 任务 13：完成开源文档、许可证和发布验证

**文件：**
- 创建：`README.md`
- 创建：`CONTRIBUTING.md`
- 创建：`SECURITY.md`
- 创建：`LICENSE`
- 创建：`docs/release-checklist.md`
- 修改：`.gitignore`

- [ ] **步骤 1：写文档验收清单**

清单必须覆盖：Fork、编辑已提交配置、创建飞书机器人、添加
`FEISHU_WEBHOOK_URL`、授予 Actions 写权限、手动试运行、首次静默基线、查看状态分支、
Cron 可能延迟、故障排查、合规与不保证购买成功。

- [ ] **步骤 2：撰写开源文件**

README 以“10 分钟开始使用”为主路径，并提供本地开发和架构说明。SECURITY 要求通过
GitHub 私密漏洞报告，不在公开 Issue 粘贴 Webhook。LICENSE 使用标准 MIT 文本，年份
为 2026，版权人先使用项目维护者占位符，并在发布前由用户确认名称。

- [ ] **步骤 3：运行文档和秘密扫描**

运行：

```powershell
rg -n "https://open.feishu.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]+" .
git diff --check
python -m pytest -q
```

预期：秘密扫描无结果，格式检查和全部测试通过。

- [ ] **步骤 4：执行本地发布候选验证**

Windows PowerShell 运行：

```powershell
python -m build
$wheel = Get-ChildItem -LiteralPath dist -Filter '*.whl' | Select-Object -First 1
python -m pip install --force-reinstall $wheel.FullName
python -m gpt_stock_monitor.cli --help
python -m gpt_stock_monitor.cli --config config/monitors.yaml --dry-run
```

Linux/bash 等价运行：

```bash
python -m build
wheel="$(find dist -maxdepth 1 -name '*.whl' -print -quit)"
python -m pip install --force-reinstall "$wheel"
python -m gpt_stock_monitor.cli --help
python -m gpt_stock_monitor.cli --config config/monitors.yaml --dry-run
```

预期：构建成功；帮助信息正常；试运行不读取飞书 Secret、不修改状态，并按线上可达性返回
0 或明确的运行错误 3。

- [ ] **步骤 5：等待用户提供 GitHub 仓库后执行真实 Fork 冒烟测试**

未经用户明确授权不得创建远端仓库、推送或发送真实飞书消息。获得授权后，严格按发布清单
执行一次全新 Fork 测试，记录日期、工作流运行链接、基线结果和状态分支提交；记录中不含
Webhook。

- [ ] **步骤 6：提交**

```powershell
git add README.md CONTRIBUTING.md SECURITY.md LICENSE docs/release-checklist.md .gitignore
git commit -m "docs: prepare open source release"
```

### 任务 14：最终质量审查与交付

**文件：**
- 修改：仅修复本项目引入且被审查发现的问题

- [ ] **步骤 1：运行完整验证**

运行：

```powershell
python -m ruff format --check .
python -m ruff check .
python -m mypy src
python -m pytest --cov=gpt_stock_monitor --cov-report=term-missing -q
git diff --check
git status --short
```

预期：所有检查通过；仅存在预期内文件；不以覆盖率数字代替关键状态机用例。

- [ ] **步骤 2：进行安全和可维护性审查**

重点检查 URL 重定向校验、子进程参数、临时工作树边界、Secret 脱敏、Pull Request Secret
隔离、待处理事件顺序、至少一次重复语义和网站选择器集中性。只修复与本项目直接相关的
问题。

- [ ] **步骤 3：重新运行受影响测试及完整验证**

预期：针对性测试和步骤 1 的完整命令全部通过。

- [ ] **步骤 4：向用户交付**

报告已完成能力、测试结果、尚未执行的真实 GitHub/飞书验证、配置入口和已知限制。未经
再次明确授权，不推送远端、不创建 Release、不发送测试消息。
