# 版本对比模块工作手册

> 状态：已交付，持续维护
> 更新日期：2026-08-09
> 适用范围：左侧导航“版本对比”及其快照、批量 Diff、结果页和 Replay

## 1. 阅读规则

日常修改先读本手册，再按变更类型读取对应源码、契约和测试。历史阶段材料集中在 `docs/archive/m2-history/`，仅用于历史审计、回归根因或契约迁移，不作为默认上下文。

当前事实来源优先级：

1. 自动化测试和当前实现；
2. `docs/contracts/` 中的契约及 ADR-006、ADR-007；
3. 本手册；
4. 历史归档。

模块名称已从阶段名切换为“版本对比”。`m2.diff.v1`、`m2.batch.v1` 等标识是已发布契约 ID，继续保留，不因模块改名而重命名。

## 2. 模块目标与边界

版本对比面向游戏策划，完成以下只读流程：

```text
选择左右 SVN 端点
-> 分别冻结当前 HEAD Revision
-> 读取两侧 Table Excel 快照
-> 生成文件级差异候选
-> 服务端按冻结 Revision 重建候选
-> 逐工作簿读取 Excel main 与同侧 TableCsv
-> 生成 m2.diff.v1
-> 在结果页按工作簿、Sheet、行、字段审阅
```

不可突破的边界：

- `source=left`、`target=right`；source-only 表示右侧删除，target-only 表示右侧新增。
- Excel 与 CSV 必须来自同一侧、同一端点、同一冻结 Revision。
- SVN Provider 只允许 `info/list/cat` 类读取；不得 commit、merge、update、copy 或写回。
- 不执行 Excel Merge，不生成写回文件，不执行宏或公式。
- Excel 只提供候选、`main` 清单和展示结构；业务值以可靠导出的 CSV 为准。
- `m2.diff.v1` 是唯一的工作簿明细 JSON；前端不得新增另一套 Diff JSON。
- 批量层只编排并保存单项摘要与 `result_ref`，不得重写语义 Diff。
- 正式结果页不得依赖 Demo 假数据。

## 3. 总体架构

```text
浏览器 /compare
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
| `/compare/demo` | 开发期交互预览 | Demo 数据，仅开发模式 |
| `/compare/demo/results` | 开发期结果预览 | Demo 数据，仅开发模式 |
| `/compare/replay` | 冻结夹具加载、黄金/当前重算结果 | `.m2fixture`，仅开发模式 |

Demo 不是正式数据源，也不是主要验收入口。共享模板或渲染器变更时应保证它不报错，但正式行为优先用 Replay 和契约测试验证。

### 4.2 文件职责

| 文件 | 职责 |
|---|---|
| `app/templates/compare.html` | 版本与快照页结构 |
| `app/static/compare.js` | 端点、快照、候选、批量创建及页面上下文 |
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

`sessionStorage` 中的 `excelDiffTaskContext` 连接输入页与结果页。工作簿“已确认”状态也保存在 `sessionStorage`，作用域按正式 task ID 或 Replay fixture ID 隔离，不写后端。

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
| `app/services/snapshot_service.py` | 端点校验、HEAD 冻结、Table 清单、文件哈希与候选 |
| `app/services/workbook_dataset_service.py` | 按请求 Revision 只读物化同侧 Excel 与 TableCsv |
| `app/services/workbook_diff_service.py` | 将两侧本地数据集编排为 `m2.diff.v1` |
| `app/services/batch_diff_service.py` | 服务端重建候选、单机调度、失败隔离、取消和重试 |
| `app/services/batch_store.py` | SQLite 状态机、租约、gzip 结果、恢复和清理 |
| `app/services/offline_batch_reader.py` | SQLite `mode=ro` 读取已完成任务供导出器使用 |
| `app/services/offline_fixture.py` | 确定性夹具、严格加载门禁、内存 Replay 与离线重算 |
| `app/services/config_service.py` | 配置读取与端点注册表管理 |
| `core/svn_provider.py` | CLI/Mock SVN 只读 Provider |

正式 API：

| 方法与路径 | 契约/用途 |
|---|---|
| `POST /api/svn/snapshots` | 冻结两侧 HEAD 并返回 Table Excel 快照 |
| `POST /api/diff/workbooks/compare` | 单工作簿请求，直接返回 `m2.diff.v1` |
| `POST /api/diff/batches` | 创建 `m2.batch.v1` 任务 |
| `GET /api/diff/batches/{task_id}` | 查询任务，支持 ETag/304 |
| `GET /api/diff/batch-results/{result_ref}` | 读取原始 `m2.diff.v1`，支持 ETag/304 |
| `POST /api/diff/batches/{task_id}/cancel` | 请求取消 |
| `POST /api/diff/batches/{task_id}/retry` | 从终态任务创建重试子任务 |

Replay API 仅在 `web.dev_mode=true` 时注册：`/api/replay/fixture`、`/api/replay/recompute`、`/api/replay/recompute/{item_id}`、`/api/replay/results/{item_id}`。

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

### 7.1 `m2.diff.v1`

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

### 7.2 `m2.batch.v1`

定义在 `app/schemas/batch.py`，完整说明、示例与验收分别位于：

- `docs/contracts/m2.batch.v1.md`；
- `docs/contracts/m2.batch.v1.example.json`；
- `docs/contracts/m2.batch.v1.acceptance.md`。

任务状态：`queued / preparing / running / cancelling / completed / completed_with_failures / cancelled / failed`。

单项状态：`queued / running / succeeded / business_failed / orchestration_failed / skipped / cancelled`。单项成功或业务失败才有 `result_ref`。批量结果引用不透明，不是路径或权限凭据。

`BatchStore` 默认写入 `var/m2-batch/batch.sqlite3`，结果写入 `var/m2-batch/results/<task>/<item>.json.gz`。启动时恢复租约和孤立文件；运行数据不进入 Git。

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

配置入口：本机运行使用被 Git 忽略的 `config/settings.json`；示例基础配置在 `config/settings.m0.example.json`。`dataset_layout` 是 Excel/CSV 结构和同侧绑定的机器可读配置。

## 10. 修改入口

| 需求类型 | 首要文件 | 必须验证 |
|---|---|---|
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
- `tests/contract/test_diff_web_mapping.py`：mapper 映射；
- `tests/contract/test_diff_json_contract.py`：`m2.diff.v1`；
- `tests/contract/test_batch_diff_api.py`：`m2.batch.v1` API；
- `tests/unit/test_offline_fixture.py`：夹具门禁与 Replay；
- `tests/unit/test_table_csv_parser.py`、`test_semantic_diff.py`：语义规则；
- `tests/unit/test_svn_workbook_dataset_resolver.py`：同侧冻结数据物化；
- `tests/integration/test_atlas_config_diff.py`：固定本地样例回归。

浏览器验收优先使用 `/compare/replay` 和当前夹具：加载后先看黄金结果，再“重算全部”并确认 55/55/0。正式任务只在用户明确授权时运行。

## 12. 已知限制与后续边界

- 当前批量运行时是本地单机 SQLite，不是分布式队列。
- 当前没有任务列表、跨任务搜索、长期报告、通知或管理后台。
- 工作簿确认态只在当前浏览器会话保存，不是多人审阅记录。
- Replay 会话只在服务进程内存中保存，重启后需重新加载夹具。
- Demo 仅用于开发预览，不能证明正式数据链路正确。
- `.xls` 旧格式没有专用业务清单解析器。
- Merge 预览、人工合入与 SVN 写回属于后续独立授权范围。

## 13. 新需求接手清单

1. 明确需求属于显示、mapper、契约、批量编排还是语义引擎。
2. 读取本手册和对应当前契约，不默认读取历史 M2 文档。
3. 用 Replay 真实样本展示当前行为；缺少覆盖时报告数据缺口，不伪造业务样本。
4. 给出最小改造方案、影响文件、风险和验收用例，等待确认。
5. 只实施确认项，保持 source/target、Revision 和 SVN 只读边界。
6. 跑对应测试、全量测试、静态检查和浏览器 Replay 验收。
7. 只有契约或语义预期变化且用户明确授权时才更新黄金夹具。
8. 若需历史背景，再进入 `docs/archive/m2-history/README.md` 按索引定向读取。
