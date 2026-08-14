# 版本对比模块工作手册

> 状态：已交付，持续维护
> 更新日期：2026-08-12
> 适用范围：左侧导航“版本对比”及其快照、批量 Diff、结果页和 Replay

## 1. 阅读规则

日常修改先读本手册，再按变更类型读取对应源码、契约和测试。历史阶段材料集中在 `docs/archive/m2-history/`，仅用于历史审计、回归根因或契约迁移，不作为默认上下文。

当前事实来源优先级：

1. 自动化测试和当前实现；
2. `docs/contracts/` 中的契约及 ADR-006、ADR-007、ADR-008；
3. 本手册；
4. 历史归档。

模块名称已从阶段名切换为“版本对比”。`m2.diff.v1`、`m2.batch.v1` 等标识是已发布契约 ID，继续保留，不因模块改名而重命名。

## 2. 模块目标与边界

版本对比面向游戏策划，完成以下只读流程：

```text
选择左右 SVN 端点
-> 每侧默认 HEAD，也可选择该分支的历史 Revision
-> 将两侧选择分别冻结为具体 Revision
-> 读取两侧 Table Excel 快照
-> 生成文件级差异候选
-> 服务端按冻结 Revision 重建候选
-> 逐工作簿读取 Excel main 与同侧 TableCsv
-> 生成 m2.diff.v1
-> 在结果页按工作簿、Sheet、行、字段审阅
```

不可突破的边界：

- `source=left`、`target=right`；source-only 表示右侧删除，target-only 表示右侧新增。
- 同一端点允许选择两个不同 Revision；相同端点与相同最终 Revision 禁止比对。
- Excel 与 CSV 必须来自同一侧、同一端点、同一冻结 Revision。
- SVN Provider 只允许 `info/list/log/cat` 类读取；不得 commit、merge、update、copy 或写回。
- 不执行 Excel Merge，不生成写回文件，不执行宏或公式。
- Excel 只提供候选、`main` 清单和展示结构；业务值以可靠导出的 CSV 为准。
- `m2.diff.v1` 是唯一的工作簿明细 JSON；前端不得新增另一套 Diff JSON。
- 批量层只编排并保存单项摘要与 `result_ref`，不得重写语义 Diff。
- 正式结果页不得依赖 Demo 假数据。

## 3. 总体架构

```text
浏览器 /compare
  -> GET /api/svn/branch-logs
  -> POST /api/svn/snapshots
  -> sessionStorage: excelDiffTaskContext
  -> POST /api/diff/batches
  -> /compare/results
  -> GET /api/diff/batches/{task_id}
  -> GET /api/diff/batch-results/{result_ref}
  -> M2DiffMapper
  -> 共享结果渲染器

开发模式 /compare/replay
  -> POST /api/replay/fixture
  -> 夹具内 m2.batch.v1 / m2.diff.v1
  -> 同一个 M2DiffMapper
  -> 同一个结果渲染器
```

后端是本地 FastAPI 单体应用。快照与 SVN 访问、单工作簿语义计算、批量编排、SQLite 持久化、Replay 服务分层实现。前端使用 Jinja2 模板、原生 JavaScript 和 CSS，没有第二套框架状态树。

运行栈见 `requirements.txt`：FastAPI、Uvicorn、Jinja2、Pydantic v2、openpyxl、pytest 和 httpx。

## 4. 页面与前端结构

### 4.1 页面路由

| 路由 | 用途 | 数据来源 |
|---|---|---|
| `/compare` | 端点选择、快照、候选、启动全量比对 | 正式 SVN/API |
| `/compare/results` | 正式批量结果页 | `m2.batch.v1` + `m2.diff.v1` |
| /compare/history | 历史任务、实时进度、任务事件、脱敏日志与全局缓存治理 | 批量管理契约 + P3 运维契约 |
| `/compare/demo` | 开发期交互预览 | Demo 数据，仅开发模式 |
| `/compare/demo/results` | 开发期结果预览 | Demo 数据，仅开发模式 |
| `/compare/replay` | 冻结夹具加载、黄金/当前重算结果 | `.m2fixture`，仅开发模式 |

Demo 不是正式数据源，也不是主要验收入口。共享模板或渲染器变更时应保证它不报错，但正式行为优先用 Replay 和契约测试验证。

### 4.2 文件职责

| 文件 | 职责 |
|---|---|
| `app/templates/compare.html` | 版本与快照页结构 |
| `app/static/compare.js` | 端点与 Revision 选择、分支 LOG 分页、快照、候选、批量创建及页面上下文 |
| pp/templates/history_tasks.html | 历史任务筛选、列表和页面状态结构 |
| pp/static/history_tasks.js | 任务分页、筛选、ETag 刷新和结果入口 |
| pp/static/history_tasks.css | 历史任务表格与响应式布局 |
| `app/templates/compare_results.html` | 正式、Demo、Replay 共用结果页结构 |
| `app/static/m2_diff_mapper.js` | 严格校验 `m2.diff.v1` 并映射为唯一前端视图模型 |
| `app/static/compare_results.js` | 工作簿/Sheet/行字段渲染、筛选、确认态、虚拟滚动和交互 |
| `app/static/compare_results_batch.js` | 轮询批量任务、刷新工作簿状态、并发读取摘要、按需读取明细 |
| `app/static/offline_replay.js` | 夹具上传、黄金/当前模式、全部或单项重算 |
| `app/static/compare_readability.css` | 输入页可读性样式 |
| `app/static/compare_results_readability.css` | 结果主区域和行字段视图样式 |
| `app/static/compare_results_batch.css` | 批量卡片、工作簿与 Sheet 导航样式 |
| `app/static/offline_replay.css` | Replay 控件样式 |

前端共享接口：

- `globalThis.M2DiffMapper`：契约到视图模型的唯一 mapper；
- `globalThis.ExcelDiffResultsBridge`：结果渲染器向批量/Replay 暴露的状态和渲染入口；
- `globalThis.ExcelDiffBatchRuntime`：任务刷新和结果加载；
- `globalThis.OfflineFixtureRuntime`：当前工作簿离线重算。

正式结果 URL 使用 `/compare/results?task_id=<UUID>`；URL Task ID 优先于 `sessionStorage`，可在关闭标签页后从“历史任务”重新发现并恢复。`sessionStorage` 中的 `excelDiffTaskContext` 继续作为旧链接兼容和当前页面缓存。工作簿“已确认”状态也保存在 `sessionStorage`，作用域按正式 task ID 或 Replay fixture ID 隔离，不写后端。

### 4.3 已验收界面基线

以下区域是当前已验收基线，除非需求明确指向，不应顺带重排：

- 顶部 `M2 BATCH TASK` 与“比对结果”是同行紧凑双卡，为 Sheet 对比留出高度；界面文案仍保留历史标签，不代表继续使用阶段工作流。
- 工作簿是页面标题下的左侧子导航，独立纵向滚动；名称隐藏 `.xlsm/.xlsx` 后缀，字号 11px。
- 工作簿默认隐藏无变化项和已确认项，标题按钮可切换显示；确认态仅是浏览器审阅状态。
- 工作簿标签显示 `+N -N`。`+N = modified_rows + target_only_rows + source_only_rows`，`-N = source_only_rows`。
- Sheet 导航横向自动换行，默认“显示修改”，可切换“显示全部”。
- Sheet `+N = modified_fields + 纯新增行展示字段数`；`-N = source_only_rows`，为 0 时不显示。
- 当前定位拼接在结果标题：`工作簿 · Sheet · 左侧第 N 行 / 右侧第 N 行`。
- 右侧“当前差异详情”模块不显示；选中字段仍由标题定位和单元格选中态表达。
- 行字段区域为左右并排网格，纵横滚动同步，行采用窗口化渲染；TARGET 侧对修改文本做字符级标红。
- 字段表头显示 CSV 第 1 条逻辑记录的显示名和第 2 条逻辑记录的字段名；可切换“显示差异/显示原表”。

## 5. 后端结构

### 5.1 Web 与依赖装配

`app/main.py` 创建 FastAPI 应用、模板路由、异常处理器和服务实例，并挂载：

- `app/api/svn.py`：端点注册、目录发现、快照及底层只读接口；
- `app/api/diff.py`：同步单工作簿比对；
- `app/api/batch.py`：批量创建、查询、取消、重试和结果读取；
- `app/api/replay.py`：开发模式夹具加载与重算；
- `app/api/health.py`：健康检查。

### 5.2 服务职责

| 文件 | 职责 |
|---|---|
| `app/services/snapshot_service.py` | 端点校验、HEAD/历史 Revision 冻结、Table 清单、文件哈希与短期可信快照复用 |
| `app/services/workbook_dataset_service.py` | 按请求 Revision 只读物化同侧 Excel 与 TableCsv |
| `app/services/workbook_diff_service.py` | 将两侧本地数据集编排为 `m2.diff.v1` |
| `app/services/batch_diff_service.py` | 服务端重建候选、单机调度、失败隔离、取消和重试 |
| `app/services/batch_store.py` | SQLite 状态机、租约、gzip 结果、恢复和清理 |
| `app/services/offline_batch_reader.py` | SQLite `mode=ro` 读取已完成任务供导出器使用 |
| `app/services/offline_fixture.py` | 确定性夹具、严格加载门禁、内存 Replay 与离线重算 |
| `app/services/config_service.py` | 配置读取与端点注册表管理 |
| `core/svn_provider.py` | CLI/Mock SVN 只读 Provider |

正式 API：

P3 运维服务由 app/services/operations_service.py 负责应用日志轮转、脱敏查询与 SVN 全局缓存治理。

| 方法与路径 | 契约/用途 |
|---|---|
| `GET /api/svn/branch-logs` | 当前分支提交 LOG 的游标分页，返回 `m2.svn-branch-log.v1` |
| `POST /api/svn/snapshots` | 每侧接受可选正整数 Revision 或 `HEAD`，默认 HEAD；返回 Table Excel 快照 |
| `POST /api/diff/workbooks/compare` | 单工作簿请求，直接返回 `m2.diff.v1` |
| `POST /api/diff/batches` | 创建 `m2.batch.v1` 任务 |
| `GET /api/diff/batches` | 查询历史任务摘要，支持游标、筛选和 ETag/304 |
| `GET /api/diff/batches/{task_id}` | 查询任务，支持 ETag/304 |
| `GET /api/diff/batches/{task_id}/management` | 查询结构化事件、正式结果统计和重试关系 |
| `DELETE /api/diff/batches/{task_id}` | 幂等删除终态任务及该任务正式结果 |
| `GET /api/diff/batch-results/{result_ref}` | 读取原始 `m2.diff.v1`，支持 ETag/304 |
| `POST /api/diff/batches/{task_id}/cancel` | 请求取消 |
| `POST /api/diff/batches/{task_id}/retry` | 从终态任务创建重试子任务 |

Replay API 仅在 `web.dev_mode=true` 时注册：`/api/replay/fixture`、`/api/replay/recompute`、`/api/replay/recompute/{item_id}`、`/api/replay/results/{item_id}`。

P3 运维 API：GET /api/operations/logs、GET /api/operations/svn-cache、POST /api/operations/svn-cache/clear。日志响应不含物理路径和堆栈；缓存响应不含缓存目录和任务归属。

## 6. 数据来源与语义引擎

### 6.1 数据配对

端点注册表定义 `TABLE` 的物理路径。每侧使用自己的 `endpoint_id + revision`：

1. 在 Table 中读取候选工作簿；
2. 从工作簿 `main` 清单读取 `sheetName`、`tbxName`、`isExport`；
3. 在 Table 同级唯一 TableCsv 目录读取 `{tbxName}.csv`；
4. 精确文件名不存在时，仅允许直接子文件中唯一的 `casefold` 完全匹配；多匹配立即失败；
5. 物化到请求级临时目录，成功或异常后清理。

不会重新读取 HEAD，不扫描全部 CSV 猜测归属，也不会跨侧借用 CSV。

### 6.2 Excel 清单

`core/workbook_manifest_parser.py` 优先使用 openpyxl 只读解析，失败时对 OOXML 做最小兜底。业务身份是 `sheetName`，`tbxName` 只定位 CSV。`main`、配置公式、业务 Sheet 单元格、样式、宏和公式不参与业务值比较。

二进制 `.xls` 可进入快照候选，但当前清单解析器没有旧版 BIFF 解析器；无法解析时会形成结构化失败结果，不得静默当作无差异。

### 6.3 CSV 定义

`core/table_csv_parser.py` 使用 Python 标准 CSV 解析器，逻辑记录定义为：

| 逻辑记录 | 含义 |
|---:|---|
| 1 | 中文/展示名 `display_name` |
| 2 | 稳定字段名 `field_name` |
| 3 | 声明类型 |
| 4 | 字段范围 `scope` |
| 8 起 | 业务数据 |

`scope=none` 的列不参与业务比较。字段名必须唯一，数据列不能越过定义宽度，空业务行跳过。

主键优先在业务字段中对配置的 `Id`、`id` 做大小写不敏感唯一匹配；没有匹配时，仅允许使用物理第一列且该列必须是有效业务字段。主键不能为空或重复。正式行匹配只使用主键值，不使用行号、内容哈希或模糊相似度。

值比较会按声明类型规范化整数、小数、布尔、date、datetime/timestamp；无法规范化时保留原字符串。输出仍保存原始展示字符串和原始逻辑行号。

### 6.4 Diff 语义

`core/semantic_diff.py` 的匹配层级：

```text
sheetName 精确匹配
-> 主键值精确匹配
-> field_name 精确匹配
```

字段状态：`common / modified / source_only / target_only`。类型或 scope 不同会使字段定义为 `modified`。行状态：`modified / source_only / target_only`。只有共享字段的规范化值不同才进入修改行的 `changes`。

工作簿状态：

- `unchanged`：所有 Sheet 无变化且无错误；
- `modified`：存在修改、source-only 或 target-only Sheet；
- `partial`：部分 Sheet 失败，仍有可读结果；
- `failed`：工作簿根错误或全部 Sheet 失败。

解析或业务错误是合法 `m2.diff.v1`，HTTP 仍可为 200；批量层分别映射为 `succeeded` 或 `business_failed`，不能把业务失败伪装成编排失败。

## 7. 数据契约

### 7.1 `m2.svn-branch-log.v1` 与快照 Revision

分支 LOG 契约位于 `docs/contracts/m2.svn-branch-log.v1.md`。每页默认 30 条，
只返回当前分支创建后的 `revision/author/date/message`；游标绑定规范化分支 URL，
损坏或跨分支复用返回 `SVN_INVALID_CURSOR`。

`POST /api/svn/snapshots` 的 `source/target` 均接受
`{"endpoint_id":"...","revision":"HEAD"}` 或正整数 Revision；省略 Revision
等价于 HEAD。HEAD 先解析一次，显式历史 Revision 不读取 HEAD，响应继续返回
`resolved_revision`。只有 HEAD 侧发现的 TABLE 路径写回端点注册表。

### 7.2 `m2.diff.v1`

定义在 `app/schemas/diff.py`，示例在 `docs/contracts/m2.diff.v1.example.json`。Pydantic 模型拒绝未知字段，规范序列化为 UTF-8、两空格缩进、结尾换行，结果 SHA-256 基于该字节序列。

主要结构：

```text
direction
workbook + workbook summary
sheets[]
  -> field definitions
  -> rows[]
     -> source/target row number and values
     -> changes[]
errors[]
```

`source_display_name`、`target_display_name` 是字段展示元数据；字段身份仍是 `name`。

### 7.3 `m2.batch.v1`

定义在 `app/schemas/batch.py`，完整说明、示例与验收分别位于：

- `docs/contracts/m2.batch.v1.md`；
- `docs/contracts/m2.batch.v1.example.json`；
- `docs/contracts/m2.batch.v1.acceptance.md`。

任务状态：`queued / preparing / running / cancelling / completed / completed_with_failures / cancelled / failed`。

单项状态：`queued / running / succeeded / business_failed / orchestration_failed / skipped / cancelled`。单项成功或业务失败才有 `result_ref`。批量结果引用不透明，不是路径或权限凭据。

`BatchStore` 默认写入 `var/m2-batch/batch.sqlite3`，结果写入 `var/m2-batch/results/<task>/<item>.json.gz`。启动时恢复租约和孤立文件；运行数据不进入 Git。

页面快照与紧随其后的批量候选准备共享同一个 `SnapshotService`。服务仍在进程内短期保存完整冻结快照事实，默认 TTL 300 秒、最多 8 对。复用键覆盖左右端点顺序、规范端点记录、冻结 Revision、M1 规则和 `dataset_layout` 配置指纹；命中时再次校验完整事实 SHA-256、端点/Revision/URL、TABLE 布局、文件范围、hash/error 组合和统计。并发相同构建使用 single-flight，失败结果不进入该缓存。

服务另在 `.cache/snapshot/` 保存可再生文件事实和可信字节，版本索引为 `index.v1.json`。文件身份同时绑定 repository UUID、规范分支 URL、规范相对路径、文件 last-changed Revision，以及 TABLE/`dataset_layout` 配置指纹；任何字段缺失或不一致都不得命中。blob 使用 SHA-256 内容寻址，读取时复核大小和哈希；索引与 blob 原子写入，相同文件并发读取使用 single-flight，并按配置的总字节、文件事实数和冻结树数执行 LRU 治理。索引损坏会清空重建，索引版本过新会只读禁用；异常和失败内容不缓存。

以上均为内部可再生优化，不属于 `m2.batch.v1` 持久化：不接受前端 HASH 或候选，不写业务 SQLite，不改变取消、任务恢复、`m2.diff.v1`、`m2.batch.v1` 或 source/target 语义。显式重试只绕过进程内整对快照缓存；重新枚举冻结目录树后，仍可在身份和树证据闭环时复用持久文件事实。内部接入点为 `SnapshotService.register_trusted_snapshot()`、`create_snapshot_at_revisions()`、`SnapshotBatchCandidateResolver.prepare_fresh()` 和脱敏诊断 `snapshot_reuse_metrics()`。

同一规范 SVN URL 第一次接触时允许完整读取；此后每次仍完整枚举冻结 `svn list --xml` 树，只在 repository UUID、路径和文件 last-changed Revision 均与持久事实一致时复用 HASH/字节。完全相同 Revision 在服务重启后内容读取为 0；跨 Revision 只读取新增或 last-changed Revision 变化的文件，删除文件由目标树自然排除。路径大小写歧义、元数据缺失、配置漂移、UUID 变化、缓存损坏或查询异常均安全完整回退，不能只凭分支 URL 相同复用。

Diff 物化读取 Excel 工作簿时优先复用同一冻结树中已校验的可信字节，避免快照完成后再次读取 SVN；CSV 仍沿用既有冻结 Revision 读取路径。本优化不改变解析、主键、配对或 Diff 语义，端到端收益必须同时报告快照与后续物化阶段，不能用页面快照耗时替代完整链路结论。

同一仓库的不同冻结 URL 还可使用只读 svn diff --summarize --xml --notice-ancestry --ignore-properties 取得两侧固定 Revision 的完整 TABLE 树差异。只有 repository UUID/root、规范 URL、冻结 Revision、TABLE 物理布局、配置指纹、两侧完整目录树和差异路径全部闭环且无大小写歧义时，未变化文件才继承 source 已有 HASH；A/M/D/R 文件和目录变化覆盖的子树一律从 target 重读。copyfrom 和 copy boundary 仅用于历史校验，不能单独证明任意两个冻结 Revision 内容相同。
命令不支持、stderr 警告、认证或权限过滤、历史截断、XML/路径异常、未知状态或证据缺项都会停止跨分支继承；该目录差异证据本身不持久化。回退后仍可按各 URL 的可信持久文件事实命中，否则完整读取内容；公共快照、m2.diff.v1、m2.batch.v1 和 source/target 语义不变。

固定 Mock 延迟验收使用 55 个基础文件、`list_tree=5ms`、`read_bytes=10ms`，每个场景独立运行 5 次并取墙钟中位数：

| 场景 | `svn list` 次数 | 内容读取数 | 墙钟中位数 | 相对冷态 |
|---|---:|---:|---:|---:|
| 首次冷态 | 2 | 60 | 0.361155s | 1.00x |
| 同进程热态 | 0 | 0 | 0.000960s | 376.2x |
| 重启后相同 Revision | 2 | 0 | 0.049834s | 7.25x |
| 重启后 5 个修改/新增 | 2 | 5 | 0.079872s | 4.52x |

实施前已有正式日志基线为最近一次 `POST /api/svn/snapshots` 41.945s、批量准备约 0.187s；固定 Mock 数据只用于可重复验收，不能伪装成真实网络环境端到端耗时。

锁定版本快照的当前内部计时事件为 `snapshot.phase_timing`，schema 为 `m2.snapshot-phase-timing.v1`，开关跟随 `operations.logging.enabled`。每个请求使用独立 `request_context_id`；实际构建者生成 `build_context_id`，并发等待者保留自己的 request context 并引用同一 build context。原始 JSONL 的 `internal_metrics` 记录请求墙钟/CPU、两侧耗时与重叠、endpoint info、递归 list、持久 lookup、Provider 读取来源与分位数、SHA-256、blob/index 原子 I/O、排序和响应构建。公开 `GET /api/operations/logs` 在模型校验前移除 `internal_metrics`，不扩展公开契约。

metrics 只允许 endpoint id、冻结 Revision、repository UUID、计数、字节和耗时，不记录凭据、完整 URL、工作簿内容或物理缓存路径。阶段墙钟包含嵌套和并行，不能直接相加；以时间区间并集的 `critical_path_accounted_seconds`、`unattributed_wall_seconds` 和 source/target `overlap_seconds` 解释请求总墙钟。

固定 Mock 四场景计时验收至少运行 10 轮：

```powershell
py -3 -m app.tools.version_comparison_snapshot_phase_timing_acceptance --rounds 10
```

`m2.batch-management.v1` 定义在 `docs/contracts/m2.batch-management.v1.md`。结构化事件独立于批量任务 JSON，默认保留 90 天；终态任务可从历史任务页手动删除。删除仅影响该任务正式结果，不级联重试任务，不触碰原始日志、全局 SVN 缓存或 Replay 夹具。

## 8. Replay 与当前夹具

当前可提交回归夹具：

| 项目 | 值 |
|---|---|
| 文件 | `var/m2-fixtures/d3c-be317423.m2fixture` |
| Task | `be317423-3863-4cfe-aa6a-fc38ad50919f` |
| Source | `KR_FIX_KR-Fix-1.0.0.0` @ r26476 |
| Target | `KR_FIX_KR-Fix-1.0.1.0` @ r26476 |
| Task 结果 | 55 succeeded，0 failed |
| 输入索引 | 728 |
| 显式缺失 | 0 |
| 黄金结果 | 55 |
| 归档 SHA-256 | `092847df4c3b97f1026fe717d789a9f676e3352f1e27b904805df06682dfb0fc` |
| 大小 | 46,218,610 bytes |
| 当前回归 | 55 current / 55 matched / 0 mismatched |

该夹具先由现有导出器从最终任务与固定 Revision 只读导出。原任务结果生成时间早于已验收的字段显示名元数据；全量核对确认 55 项的 6414 处差异全部且仅为 `source_display_name/target_display_name`。经用户授权，随后使用同一夹具内 728 个冻结输入离线重算黄金结果并同步结果哈希。任务 ID、端点、Revision、候选范围和输入字节未改变。它是当前代码回归基线，不宣称黄金 JSON 与批量数据库中最初保存的结果逐字节相同。

夹具格式 `m2.fixture.v1` 是确定性 ZIP，包含 manifest、输入索引、缺失清单、内容寻址 blob、任务、黄金结果和审计项。加载器校验路径、压缩格式、大小、成员集合、SHA-256、契约和任务身份。

更新规则：

- 只有用户明确授权后才可导出或重定黄金；
- 优先使用已有终态任务，不启动新正式任务；
- 导出器只读 SQLite，并按任务固定 Revision 读取 SVN；
- 黄金变化必须先展示 mismatch、分类差异并记录原因；
- 不得伪造缺失输入、业务样本或结果；
- `.gitignore` 只放行当前夹具，旧夹具由 Git 历史保留。

## 9. 工具与脚本分类

| 分类 | 脚本 | 用途 |
|---|---|---|
| 正式服务 | `py -3 -m app.main` | 启动 FastAPI/Uvicorn |
| 本地语义样例 | `app/tools/diff_sample.py` | 对本地左右目录生成规范 Diff JSON |
| Web 单项样例 | `app/tools/diff_web_sample.py` | 开发期单工作簿 Web 样例 |
| Web 批量样例 | `app/tools/batch_web_sample.py` | 开发期批量状态样例，不是正式任务 |
| 夹具导出 | `app/tools/export_offline_fixture.py` | 从终态任务导出冻结输入和结果，会只读 SVN |
| 历史验证 | `docs/verify/` | 早期第三方库风险实验，仅审计时使用 |
| AI 配置工具 | `app/tools/*ai*`、`scan_ai_field_catalog.py` | 邻接能力，不属于版本对比 Diff 主链路 |

配置入口：本机运行使用被 Git 忽略的 `config/settings.json`；示例基础配置在 `config/settings.m0.example.json`。`dataset_layout` 是 Excel/CSV 结构和同侧绑定的机器可读配置。`snapshot_reuse.ttl_seconds` 和 `snapshot_reuse.max_entries` 控制页面冻结快照的进程内复用窗口；任一设为 0 可关闭复用并保留完整重建路径。

2026-08-13 离线性能基线使用 24 个 Excel/侧、Provider `list_tree=40ms`、`read_bytes=10ms`、7 轮中位数：冷准备 85.528ms（2 次 list、48 次 read）；页面已锁定但禁用复用 44.557ms（2 次 list、0 次 read）；可信热复用 0.808ms（0 次 list、0 次 read），相对页面后的原准备阶段降低约 98.2%。三组候选均为 24 项且规范候选 JSON/指纹一致。该基线只用于比较候选准备开销，不替代真实 SVN 环境验收。

## 10. 修改入口

| 需求类型 | 首要文件 | 必须验证 |
|---|---|---|
| 端点 Revision/分支 LOG | `svn_provider.py`、SVN schema/service/API、`compare.js` | 分支 URL、分页游标、stop-on-copy、过期请求、同端点合法性 |
| 纯布局/样式 | 结果模板与对应 CSS | Replay 桌面视口、契约页面测试 |
| 工作簿/Sheet/网格交互 | `compare_results.js` | Replay 代表工作簿、键盘/滚动/筛选、Node 语法 |
| 批量状态和自动刷新 | `compare_results_batch.js` | queued/running/terminal、摘要延迟、失败与重试 |
| 契约到视图映射 | `m2_diff_mapper.js` | mapper 契约测试；不得改出第二 JSON |
| 单工作簿 API | `app/api/diff.py`、dataset/diff service | 冻结 Revision、临时目录清理、业务失败 HTTP 200 |
| 批量 API/状态机 | batch schema/service/store/API | 幂等、租约、恢复、失败隔离和契约测试 |
| CSV/主键/Diff 语义 | `core/` + diff schema/service | 先确认语义变更；单元、AtlasConfig、全量 Replay |
| 夹具 | exporter/offline fixture | 输入完整性、SHA、55/55/0；需显式授权 |

不要从 CSS 问题顺带改 mapper，不要从显示统计反推修改 `m2.diff.v1.summary`，不要为 Demo 添加正式页面依赖。

## 11. 验证清单

基础静态检查：

```powershell
node --check app/static/compare.js
node --check app/static/compare_results.js
node --check app/static/compare_results_batch.js
node --check app/static/m2_diff_mapper.js
node --check app/static/offline_replay.js
git diff --check
```

自动化测试：

```powershell
py -3 -m pytest -q
```

按风险选择重点用例：

- `tests/contract/test_compare_preview.py`：模板、资源和结果页界面契约；
- `tests/contract/test_svn_api.py`、`tests/unit/test_svn_provider.py`：分支 LOG、游标、CLI 边界与快照请求；
- `tests/contract/test_snapshot_service.py`：HEAD/历史 Revision 冻结、同分支合法性和 TABLE 路径回退；
- `tests/contract/test_diff_web_mapping.py`：mapper 映射；
- `tests/contract/test_diff_json_contract.py`：`m2.diff.v1`；
- `tests/contract/test_batch_diff_api.py`：`m2.batch.v1`、`m2.batch-list.v1`、`m2.batch-management.v1` API 与恢复/清理；
- `tests/unit/test_snapshot_batch_reuse.py`：页面快照直达批量候选、single-flight、过期/重启/配置变化/损坏回退、取消和资源回收；
- `tests/unit/test_offline_fixture.py`：夹具门禁与 Replay；
- `tests/unit/test_table_csv_parser.py`、`test_semantic_diff.py`：语义规则；
- `tests/unit/test_svn_workbook_dataset_resolver.py`：同侧冻结数据物化；
- `tests/integration/test_atlas_config_diff.py`：固定本地样例回归。

浏览器验收优先使用 `/compare/replay` 和当前夹具：加载后先看黄金结果，再“重算全部”并确认 55/55/0。正式任务只在用户明确授权时运行。

## 12. 已知限制与后续边界

- 当前批量运行时是本地单机 SQLite，不是分布式队列。
- 当前已有保留期内任务列表、Task URL 恢复、结构化任务事件、终态结果管理、脱敏应用日志检索和全局 SVN 缓存治理；仍没有长期报告或通知。
- 工作簿确认态只在当前浏览器会话保存，不是多人审阅记录。
- Replay 会话只在服务进程内存中保存，重启后需重新加载夹具。
- Demo 仅用于开发预览，不能证明正式数据链路正确。
- `.xls` 旧格式没有专用业务清单解析器。
- Merge 预览、人工合入与 SVN 写回属于后续独立授权范围。

### 12.1 待办：日志与缓存自动治理

当前 `.cache/svn/` 没有自动清理，开发日志也没有统一轮转。后续应由版本对比应用实现受控治理，避免依赖人工删除或 Windows 计划任务直接清目录。

清理边界：

- `.cache/svn/` 是可再生的 SVN 只读内容缓存，不是业务真值；缓存未命中时必须按原端点和冻结 Revision 重新读取，不得改读 HEAD。
- `.cache/*.log` 是开发排障日志，可以轮转或删除，但删除后会失去对应历史诊断信息。
- 手工清理只能在没有进行中任务且相关服务已停止时执行；运行中不得直接删除 `.cache` 目录。
- 自动清理必须避开正在读取、写入及刚生成的文件；删除失败只能记录并稍后重试，不得使正式比对失败。
- `var/m2-batch/batch.sqlite3`、`var/m2-batch/results/`、`var/m2-fixtures/`、配置和契约不属于缓存治理范围，严禁按 `.cache` 规则删除。
- 删除 SVN 缓存不影响已经持久化的批量结果，但会使后续任务重新访问 SVN；SVN 不可用时，缓存未命中的任务可能无法执行。

建议实现规则（实施前需再次确认）：

- 新增应用管理的 SVN 磁盘缓存管理器，采用“容量上限 + LRU + 空闲过期”策略。
- 默认最大容量 5 GiB，触发清理后收缩到 4 GiB；连续 14 天未访问的条目允许过期。
- 每小时检查一次；磁盘剩余空间低于 10 GiB 时立即触发清理。
- 最近 10 分钟内生成或访问的文件不得删除。
- 缓存写入使用临时文件和原子替换；使用独立索引记录大小与最后访问时间，并支持从磁盘扫描重建索引。
- 多进程环境必须使用跨进程锁；占用中的文件跳过并重试。
- 开发日志采用按大小或日期轮转，并设置保留数量或总容量上限；具体阈值在实施时结合实际日志量确定。

验收要求：

- 进行中任务不受清理影响，冻结 Revision、`source=left`、`target=right` 语义保持不变。
- 清理后缓存未命中能够从同一冻结 Revision 重建，结果与清理前一致。
- 已完成任务及其 `m2.batch.v1`、`m2.diff.v1` 结果仍可正常查询和展示。
- 并发访问、文件占用、索引缺失、删除失败和磁盘空间不足均有自动化覆盖；清理失败不得阻断正式任务。
- 提供缓存占用、清理数量、释放空间和失败数量的可观测日志。

## 13. 新需求接手清单

1. 明确需求属于显示、mapper、契约、批量编排还是语义引擎。
2. 读取本手册和对应当前契约，不默认读取历史 M2 文档。
3. 用 Replay 真实样本展示当前行为；缺少覆盖时报告数据缺口，不伪造业务样本。
4. 给出最小改造方案、影响文件、风险和验收用例，等待确认。
5. 只实施确认项，保持 source/target、Revision 和 SVN 只读边界。
6. 跑对应测试、全量测试、静态检查和浏览器 Replay 验收。
7. 只有契约或语义预期变化且用户明确授权时才更新黄金夹具。
8. 若需历史背景，再进入 `docs/archive/m2-history/README.md` 按索引定向读取。
