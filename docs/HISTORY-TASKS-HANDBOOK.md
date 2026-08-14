# 历史任务模块维护手册

> 状态：P1-P3 已交付，持续维护
> 更新日期：2026-08-09
> 页面入口：/compare/history
> 适用范围：历史任务恢复、任务管理、运行日志、SVN 全局缓存治理

## 1. 文档用途

本文是“历史任务”模块后续修 Bug 和新增功能的首要维护入口。它描述当前源码事实、数据边界、常见故障定位和修改后的必测项。

HISTORY-TASKS-IMPLEMENTATION.md 是阶段实施与验收记录；本手册是长期维护规则。涉及 Diff 语义、数据配对或 Replay 时，还必须读取 VERSION-COMPARISON-HANDBOOK.md 和对应数据契约。

## 2. 模块目标

历史任务模块解决三类问题：

1. 标签页关闭后仍能重新发现保留期内任务。
2. 重新进入活动任务时挂接原 Task ID，重新进入终态任务时读取既有结果。
3. 提供与任务生命周期分离的脱敏应用日志和全局 SVN 可再生缓存治理入口。

页面顶层视图：

| 视图 | 数据来源 | 主要动作 |
|---|---|---|
| 任务记录 | 批量 SQLite 与正式结果摘要 | 筛选、分页、恢复、查看事件、删除终态任务 |
| 运行日志 | 进程 JSONL 日志 | 脱敏查询、分页、相关性检索 |
| SVN 缓存 | 全局缓存目录与进程内计数 | 查看容量/命中、独立全局清理 |

## 3. 不可突破的边界

- 保持 m2.diff.v1、m2.batch.v1、source=left、target=right 不变。
- 保持同侧 Excel/TableCsv、两侧独立冻结 Revision 和 SVN 只读。
- 恢复任务只允许 GET 既有任务；不得因为上下文缺失重新 POST 创建任务。
- URL Task ID 是正式恢复入口，优先于 sessionStorage。
- 正式结果文件是任务产物，不是缓存。
- 结构化任务事件是审计数据，不是原始应用日志。
- 原始应用日志按进程治理，不随任务删除。
- SVN 缓存跨任务共享且可再生，不存在“某任务的缓存”。
- Replay .m2fixture 是回归资产，不进入任务、日志或缓存自动清理。
- 不新增第二套前端 Diff JSON，不从历史页修改 core 解析、配对、主键或 Diff 语义。

## 4. 架构与调用链

~~~text
/compare/history
  ├─ 任务记录
  │   ├─ GET /api/diff/batches
  │   ├─ GET /api/diff/batches/{task_id}/management
  │   ├─ DELETE /api/diff/batches/{task_id}
  │   └─ /compare/results?task_id={task_id}
  ├─ 运行日志
  │   └─ GET /api/operations/logs
  └─ SVN 缓存
      ├─ GET /api/operations/svn-cache
      └─ POST /api/operations/svn-cache/clear
~~~

后端职责：

~~~text
batch.py
  -> BatchDiffService
  -> BatchStore
  -> SQLite + 正式 gzip 结果

operations.py
  -> OperationalLogService
  -> 按日期/PID/大小轮转的 JSONL

operations.py
  -> SVNCacheService
  -> 全局 rev_*__<md5>.bin + SVNClient 会话指标
~~~

## 5. 文件职责

| 文件 | 维护职责 |
|---|---|
| app/templates/history_tasks.html | 三个顶层视图、任务详情与缓存确认对话框 |
| app/static/history_tasks.js | URL 状态、查询、分页、ETag、自动刷新和交互 |
| app/static/history_tasks.css | 桌面/移动端表格、日志与缓存布局 |
| app/api/batch.py | 历史任务、管理详情、删除、重试、结果读取 API |
| app/schemas/batch.py | 批量列表、管理和删除契约 |
| app/services/batch_diff_service.py | API 到存储层的任务编排 |
| app/services/batch_store.py | SQLite 状态、事件、墓碑、正式结果生命周期 |
| app/api/operations.py | 日志查询与 SVN 缓存治理 API |
| app/schemas/operations.py | P3 严格响应/请求模型 |
| app/services/operations_service.py | 日志轮转/脱敏/分页及缓存统计/清理 |
| core/svn_client.py | SVN 内容缓存读写和进程会话命中计数 |
| app/main.py | 服务装配、请求相关 ID、中间件、目录配置 |

核心测试：

| 测试 | 覆盖范围 |
|---|---|
| tests/contract/test_batch_diff_api.py | 列表、事件、删除、墓碑、恢复、生命周期隔离 |
| tests/contract/test_operations_api.py | 日志脱敏/分页/ETag、缓存状态和隔离清理 |
| tests/contract/test_compare_preview.py | 页面结构、资源版本、URL 恢复契约 |
| tests/unit/test_operations_service.py | 日志日期/PID/大小轮转和保留期 |
| tests/unit/test_svn_cache_metrics.py | 内存/磁盘命中和内存索引清空 |

## 6. 数据与生命周期

| 数据 | 所有权 | 默认生命周期 | 删除入口 |
|---|---|---|---|
| 批量任务元数据 | 单任务 | 终态 30 天 | 任务删除/过期清理 |
| 正式 gzip 结果 | 单任务 | 随任务 30 天 | 仅该任务删除/过期 |
| 删除墓碑 | 单任务 | 删除后 7 天 | 自动过期 |
| 结构化任务事件 | Task ID 关联、独立表 | 90 天 | 独立保留策略 |
| 原始应用日志 | 服务进程/实例 | 默认 14 天并限制文件数 | 日志轮转 |
| SVN 磁盘缓存 | 全局共享 | 可再生 | 独立全局确认 |
| SVN 内存索引 | 当前进程 | 服务重启前 | 重启或全局清理 |
| 快照事实/字节缓存 | 全局共享、内部可再生 | 配置化 LRU 容量/条目上限 | 自身治理 |
| Replay 夹具 | 版本化回归资产 | 长期 | 仅显式人工维护 |

快照事实/字节缓存独立位于 `.cache/snapshot/`，不写业务 SQLite，也不属于 `/api/operations/svn-cache` 对 `.cache/svn` 的状态查询或清理范围。它通过 `index.v1.json`、内容寻址 blob 和自身 LRU 上限治理；删除任务、清理 SVN 读取缓存或恢复任务都不得顺带删除它。

任务删除必须满足：

- 仅终态任务可删除，活动任务返回 409。
- 相同 request ID 幂等。
- 父任务和重试子任务不级联。
- 只删除该任务正式结果。
- 任务读取和结果读取立即返回 410；墓碑结束后返回 404。
- 不触碰结构化事件保留策略、日志、SVN 缓存和 Replay。

## 7. 页面状态与 URL

任务视图参数：

- group：all / active / completed / attention
- q：Task ID 或端点关键词
- from / to：创建日期
- detail：打开的 Task ID

日志视图参数：

- view=logs
- log_level
- task_id / request_id
- log_q
- log_from / log_to

缓存视图参数：

- view=cache

正式结果恢复地址：

/compare/results?task_id=<UUID>

恢复优先级：

1. 校验 URL Task ID。
2. 通过 GET 读取既有任务并重建页面上下文。
3. URL 不含 Task ID 时，才使用 sessionStorage 兼容旧链接。
4. 410 显示过期，404 显示不存在，不创建替代任务。

自动刷新：

- 页面隐藏时暂停。
- 活动任务 2 秒；仅终态任务 15 秒。
- 日志 5 秒；缓存状态 15 秒。
- ETag 命中返回 304，保留现有 DOM。

## 8. API 契约

任务列表 m2.batch-list.v1：

- 游标分页，默认 created_at DESC, task_id DESC。
- 只返回摘要，不返回 items 明细、result_ref、内部路径、SVN URL 或凭据。
- 筛选条件必须参与游标签名，错误游标返回公开错误。

任务管理 m2.batch-management.v1：

- 返回任务状态、正式结果计数/规范 JSON 字节数、到期时间、重试关系和事件。
- 不复制 m2.diff.v1，不返回正式结果路径。
- 旧任务没有原生事件时，从权威时间戳生成最小基线事件。

日志 m2.operations-log-list.v1：

- 按 created_at DESC, event_id DESC。
- 支持级别、关键词、时间、Task ID、request ID。
- 返回字段经过严格模型校验和再次脱敏。
- 不返回日志文件名、文件路径、异常对象或堆栈。

缓存 m2.svn-cache-status.v1：

- scope 固定 global_shared，reproducible 固定 true。
- 只返回受管文件数量/大小、未知项数量和当前进程命中指标。
- 不返回缓存目录、缓存文件名、SVN 地址或任务归属。

详细字段以 docs/contracts 下三个 batch 契约和 m2.operations.v1.md 为准。

## 9. 日志实现规则

日志文件格式：

excel-merge-YYYYMMDD-p<PID>-NNN.jsonl

- 日期隔离每日文件。
- PID 隔离并行服务进程。
- NNN 在达到 max_bytes 后递增。
- 默认日志目录 var/logs，默认单文件 5 MiB、保留 14 天、最多 200 个文件。
- 查询默认最多扫描新近 64 MiB，防止页面查询无界读取。

公开字段：

- event_id、created_at、level、logger、event、message
- request_id、task_id、process_id

原始进程 JSONL 允许事件携带经过递归限深、限量和脱敏的 `internal_metrics`，用于快照性能排障等内部诊断。`OperationalLogService` 在公开日志模型校验前必须移除该字段；`GET /api/operations/logs`、前端日志视图和 `m2.operations-log-list.v1` 均不得暴露或新增该字段。


脱敏发生在写入前和查询时，至少覆盖：

- password/token/secret/authorization/credential 等键值。
- URL 用户信息。
- Windows/UNC/file URI 内部路径。
- SVN/SVN+SSH 地址。
- Traceback、File 行和异常堆栈。

新增日志时：

- 使用 app 命名空间 logger，确保进入运维 handler。
- event 使用稳定的小写点号命名，例如 batch.retry_created。
- Task ID/request ID 放入 logging extra，不把结构化字段拼成自由文本后再解析。
- message 只写公开摘要，禁止写请求体、配置对象、凭据、结果 JSON 和异常堆栈。

## 10. SVN 缓存治理规则

当前受管磁盘格式：

rev_<revision>__<md5>.bin

全局清理：

- 固定确认文本为“清空全局 SVN 缓存”。
- 使用 request ID 提供当前进程内幂等。
- 只删除缓存目录直接子级中匹配当前格式的普通文件。
- 不递归，不跟随符号链接，不删除未知文件或目录。
- 同时清空当前 SVNClient 内存索引。
- 文件系统根、.git、var/m2-batch、var/m2-fixtures 及其危险父子目录禁止清理。

命中指标是进程会话指标，服务重启后归零。缓存容量来自磁盘实际受管文件。Mock Provider 下缓存显示未启用；CLI Provider 只有实际 read_bytes/read_content 才会产生命中或未命中，不得为了验收主动访问 SVN。

## 11. 常见故障定位

### 11.1 历史页没有任务

依次检查：

1. GET /api/diff/batches 是否返回 m2.batch-list.v1。
2. EXCEL_MERGE_BATCH_STATE_DIR 是否指向预期状态目录。
3. 当前工作区是否只有空的默认 var/m2-batch。
4. 任务是否已到期返回 410。
5. 页面筛选参数是否排除了全部任务。

不要通过新建任务来“验证列表”，优先使用隔离数据库副本。

### 11.2 历史任务能看见但结果打不开

检查：

1. URL 是否包含合法 task_id。
2. GET /api/diff/batches/{task_id} 是 200、410 还是 404。
3. task.items 中成功/业务失败项是否仍有 result_ref。
4. GET /api/diff/batch-results/{result_ref} 是否命中正式 gzip 结果。
5. 结果页是否误用 sessionStorage 覆盖 URL Task ID。

### 11.3 自动刷新不更新

检查 document.visibilityState、pollTimer、当前 view、ETag key 是否与完整查询 URL 一致。追加分页不能覆盖首屏 ETag；切换筛选必须清空旧 cursor 和 ETag。

### 11.4 旧任务没有时间线

BatchStore 会基于任务权威时间戳补充“创建 + 当前终态”基线。若缺失，检查 task_events 表初始化、ensure_baseline_events 和时间戳是否合法，不要伪造业务事件。

### 11.5 日志查不到

检查：

1. operations.logging.enabled。
2. EXCEL_MERGE_LOG_DIR 或 operations.logging.directory。
3. logger 名称是否在 app 命名空间。
4. Task ID/request ID 是否作为 extra 写入。
5. 时间筛选的时区和 max_scan_bytes。

日志查询和缓存状态 GET 不自记录，避免污染结果和持续改变 ETag。

### 11.6 缓存一直为 0

- Mock Provider 正常显示未启用。
- CLI Provider 在未读取 SVN 内容时，空缓存和零命中是正常状态。
- 会话指标重启后归零。
- 检查 EXCEL_MERGE_SVN_CACHE_DIR 与 svn.cache_dir 是否一致。

### 11.7 删除影响范围异常

立即检查 manual_deletions、tasks/items、结果引用和实际 gzip 文件。任何日志、.cache/svn、其他任务结果或 .m2fixture 的变化都属于严重回归。

## 12. 新增功能的改动路径

新增任务筛选：

1. 扩展 API Query 和 BatchStore 查询。
2. 将筛选值纳入游标签名。
3. 更新 URL 持久化和前端重置。
4. 增加分页无重复遗漏与 ETag 测试。

新增结构化任务事件：

1. 选择稳定 event_type 和公开 message。
2. details 只放白名单标量，不放原始异常或结果。
3. 在同一事务或明确的状态转换边界写入。
4. 验证旧任务基线和事件保留期。

新增日志字段：

1. 先扩展严格 schema。
2. 明确脱敏规则和最大长度。
3. 保持文件路径、URL、凭据和堆栈不进入响应。
4. 增加写入前与查询时脱敏测试。

新增缓存类型：

1. 先确认是否全局共享且可再生。
2. 定义精确受管文件格式，未知项默认保留。
3. 清理必须独立确认并拒绝危险目录。
4. 使用隔离副本验证，不在 main 缓存上执行删除。

新增长期报告或通知：

这是后续独立能力，不应复用原始日志或 SVN 缓存作为持久业务存储。先定义所有权、保留期、权限和独立契约。

## 13. 配置与本地验收

相关配置：

| 配置 | 默认值 | 用途 |
|---|---|---|
| batch_diff.event_retention_days | 90 | 结构化事件保留 |
| operations.logging.enabled | true | 是否写应用日志 |
| operations.logging.directory | var/logs | 日志目录 |
| operations.logging.max_bytes | 5242880 | 单日志文件大小 |
| operations.logging.retention_days | 14 | 日志保留天数 |
| operations.logging.max_files | 200 | 日志文件上限 |
| operations.logging.max_scan_bytes | 67108864 | 单次查询扫描上限 |
| operations.allow_cache_clear | true | 是否允许全局缓存清理 |

验收隔离环境变量：

- EXCEL_MERGE_BATCH_STATE_DIR
- EXCEL_MERGE_LOG_DIR
- EXCEL_MERGE_SVN_CACHE_DIR
- EXCEL_MERGE_SVN_PROVIDER

环境变量只影响当前服务进程。使用 CLI Provider 做缓存启用态验收时，不得调用快照、内容读取或新建正式任务。

## 14. 验证清单

静态检查：

~~~powershell
node --check app/static/history_tasks.js
py -3 -m py_compile app/api/operations.py app/schemas/operations.py app/services/operations_service.py
git diff --check
git diff --cached --check
~~~

自动化：

~~~powershell
py -3 -m pytest -q tests/contract/test_batch_diff_api.py
py -3 -m pytest -q tests/contract/test_operations_api.py
py -3 -m pytest -q tests/contract/test_compare_preview.py
py -3 -m pytest -q tests/unit/test_operations_service.py tests/unit/test_svn_cache_metrics.py
py -3 -m pytest -q
~~~

浏览器：

- 桌面 1440×900 和移动 390×844 无水平溢出。
- 三个 view 的 URL 刷新后可恢复。
- 任务筛选、分页、详情、结果入口和错误状态正常。
- 日志筛选、分页、脱敏标签和空状态正常。
- 缓存启用/禁用/空状态、确认文本和按钮状态正常。
- 控制台无错误。
- 缓存删除只在隔离缓存副本执行。

真实历史数据：

- 复制 SQLite 与其引用结果到隔离目录。
- 校验任务数、状态数、结果数、规范 JSON 字节数和 SHA-256。
- 打开既有结果时确认创建任务 API 调用为 0。
- 不访问 SVN，不更新 Replay 黄金夹具，不删除 main 数据。

## 15. 兼容与迁移

- task_events 和 manual_deletions 由 SQLite 初始化幂等创建，不需要离线迁移。
- 旧任务通过权威时间戳补基线事件，不改写 m2.batch.v1。
- P3 日志从功能启用后开始记录，不迁移旧原始日志。
- P3 缓存指标只统计当前 SVNClient 进程，不回填历史命中。
- 裸 /compare/results 继续兼容 sessionStorage；新入口必须携带 URL Task ID。

## 16. 相关文档

- docs/HISTORY-TASKS-IMPLEMENTATION.md：P1-P3 分阶段实施和真实数据验收。
- docs/VERSION-COMPARISON-HANDBOOK.md：版本对比全模块边界。
- docs/contracts/m2.batch-list.v1.md：历史列表契约。
- docs/contracts/m2.batch-management.v1.md：管理详情契约。
- docs/contracts/m2.operations.v1.md：日志与缓存契约。
- docs/contracts/m2.batch.v1.md：正式批量任务契约。
- docs/contracts/m2.diff.v1.example.json：唯一工作簿 Diff JSON 示例。
