# M3 版本监控实施计划

> 状态：草案，依据 `M3-VERSION-MONITORING-PRD.md`
> 更新日期：2026-08-10
> 执行约束：本文件只定义实施顺序和验收门禁；正式编码应在独立 worktree 中进行。

## 1. 实施原则

- 版本监控是独立模块，不修改 `m2.diff.v1`、`m2.batch.v1` 或现有版本对比方向语义；
- 优先复用 TableCsv 解析、类型规范化、主键和语义 Diff，不复用旧 `core/attributor.py`、`core/differ.py` 和 `core/report_html.py` 作为正式实现；
- Windows 计划任务只是触发器，SQLite 才是任务、区间、运行和同步状态的事实来源；
- Runner 必须可在不启动 FastAPI 的情况下独立装配和执行；
- 所有区间、Revision、产物路径和重试都必须确定且幂等；
- 新契约使用严格模型并拒绝未知字段；
- 先完成核心算法和持久化，再接 Windows 调度和页面。

## 2. 目标架构

```text
/monitor、/monitor/tasks
  -> /api/monitor/tasks、runs、reports
  -> MonitorTaskService
  -> MonitorStore (SQLite)
  -> WindowsSchedulerGateway

Windows Task Scheduler
  -> python -m app.monitor_runner --task-id <UUID>
  -> MonitorDueRunService
  -> BranchHistoryService
  -> MonitorDiffService
  -> MonitorAttributionService
  -> MonitorReportPublisher
  -> var/m3-monitor

BranchHistoryService
  -> SVN CLI info/log/cat（只读）

MonitorDiffService
  -> workbook_manifest_parser
  -> table_csv_parser
  -> semantic_diff
```

建议新增模块：

| 模块 | 职责 |
|---|---|
| `app/schemas/monitor.py` | M3 严格 API 和产物契约 |
| `app/api/monitor.py` | 任务、运行、报告和调度同步 API |
| `app/services/monitor_store.py` | SQLite 状态、边界、运行、attempt 和结果引用 |
| `app/services/monitor_task_service.py` | 任务生命周期与 API 编排 |
| `app/services/monitor_schedule.py` | 时区、计划截止点、暂停/恢复/结束边界计算 |
| `app/services/windows_scheduler.py` | Windows 计划任务抽象、真实适配器和漂移检测 |
| `app/services/branch_history_service.py` | 分支身份、日期 Revision、日志和 changed path 过滤 |
| `app/services/monitor_diff_service.py` | 起止快照最终净值计算 |
| `app/services/monitor_attribution_service.py` | 逐提交字段事件回放及最终差异归因 |
| `app/services/monitor_report_service.py` | 规范结果、离线 HTML、原子发布和 30 天治理 |
| `app/monitor_runner.py` | 无 Web 依赖的独立执行入口 |
| `app/templates/monitor.html` | 新建任务和模块概览 |
| `app/templates/monitor_tasks.html` | 当前任务、运行状态和操作 |
| `app/static/monitor*.js/css` | 版本监控前端交互与样式 |

文件名可在实现时按现有模块粒度微调，但职责边界不得合并回 M2 BatchStore 或 OperationsService。

## 3. 契约设计

实施前先冻结以下契约：

| 契约 | 用途 |
|---|---|
| `m3.monitor-task.v1` | 单个监控任务、固定分支和调度状态 |
| `m3.monitor-task-list.v1` | 当前任务列表、筛选和分页 |
| `m3.monitor-run.v1` | 单个逻辑截止点的运行、attempt 和报告摘要 |
| `m3.monitor-report.v1` | 最终净值、字段归因、覆盖范围和错误 |

建议请求/响应不返回 SVN 凭据、Windows 密码、仓库根目录 Revision 列表、物理路径、命令行完整 stderr、原始异常或堆栈。

报告契约应区分：

- `field_modified`；
- `row_added`；
- `row_deleted`；
- `field_added`；
- `field_removed`；
- `field_definition_modified`。

三类字段结构变化使用 `row_key=null`，不计入 `changed_row_count`；不得使用哨兵值或按业务行展开。`field_modified / row_added / row_deleted` 仍必须携带非空业务主键。

每项字段变化保存规范身份、原始展示值、归一化比较依据和最终归因；HTML 只消费这一份契约，不再派生第二套业务 JSON。

## 4. 持久化模型

建议使用独立 `var/m3-monitor/monitor.sqlite3`，至少包含以下表。

### 4.1 `monitor_tasks`

- Task ID、名称、状态；
- endpoint ID、仓库 UUID、规范 URL 摘要和仓库相对分支路径；
- 时区、生效时间、结束时间、每日触发时间；
- 调度 generation、期望状态、实际同步状态和公开错误码；
- Windows task name、最近同步时间和最近心跳；
- 创建、更新、暂停、恢复、结束和归档时间。

禁止保存 Windows 密码和 SVN 凭据。列表 API 只返回已注册端点身份和公开标签。

### 4.2 `monitor_boundaries`

记录完整区间链，避免依赖当前配置反推历史：

- `start`：任务生效起点，不生成报告；
- `scheduled`：每日计划截止点，生成报告；
- `pause`：暂停截止点，生成报告；
- `resume`：恢复起点，不生成报告；
- `end`：任务最终截止点，生成报告。

每个边界保存 UTC 时间、本地显示时间、schedule generation 和产生原因。`(task_id, boundary_at, boundary_type)` 必须幂等。

### 4.3 `monitor_runs` 与 `monitor_run_attempts`

`monitor_runs` 以 `(task_id, logical_cutoff_at)` 唯一，保存：

- 区间起止、截止类型和计划时间；
- 状态、租约、进度和公开错误摘要；
- 起止有效分支 Revision；
- 结果引用、HTML 相对路径、SHA-256 和统计；
- 创建、开始、完成和过期时间。

`monitor_run_attempts` 保存每次自动或人工尝试的分类、时间、结果和可公开错误码。自动重试不能创建第二个逻辑 Run。

### 4.4 事务与恢复

- 使用 SQLite WAL、外键和显式事务；
- Runner 通过租约抢占 Run，重复系统触发只允许一个执行者；
- 启动时恢复过期租约，不能并发重写同一产物；
- Scheduler sync 使用 generation 防止旧更新覆盖新配置；
- API 更新业务期望状态后同步 Windows 任务，失败保留 `scheduler_error`，不能丢失用户配置；
- Windows 旧触发启动 Runner 后必须重新读取数据库，发现暂停、结束、generation 过期或无到期边界时正常退出。

## 5. Windows 计划任务

### 5.1 抽象

定义 `SchedulerGateway`，至少提供 create/update、enable/disable、delete、inspect 和 validate。单元与契约测试使用 Fake Gateway；Windows 真实适配器不得进入普通单元测试路径。

### 5.2 系统任务规则

- 每个 Monitor Task 对应一个稳定系统任务名，名称只包含固定前缀和 UUID；
- Action 使用当前解释器绝对路径、`-m app.monitor_runner`、Task ID 和固定工作目录；
- 每日触发时间与业务配置一致；
- 增加当前用户登录触发，用于补跑注销或关机期间遗漏截止点；
- 设置 missed start、失败重启、最长运行时间和重复实例策略；
- 临时错误由系统每 10 分钟重启，最多 3 次；
- 确定性错误由 Runner 记录后正常退出，避免系统无效重启；
- 提供独立每日维护唤醒，在 Web 关闭且没有活动监控任务时仍调用全任务报告保留治理；
- 暂停禁用系统任务，结束或归档移除或禁用系统任务；
- 页面必须能检测任务缺失、Action 漂移、触发时间不一致和运行身份错误。

### 5.3 安全边界

- 第一版只支持当前登录用户，不请求或保存登录密码；
- 命令参数不能包含 SVN URL、凭据或报告物理路径；
- Task ID 必须严格 UUID 校验；
- 生成系统任务定义时使用结构化 XML/API，不拼接不受控 Shell 文本；
- Windows 任务操作失败必须映射为稳定公开错误码。

## 6. SVN 历史读取

### 6.1 独立历史接口

不要让 M3 直接依赖旧 `SVNClient.fetch_revisions`。新增面向固定分支的只读接口：

- resolve branch identity；
- resolve last branch revision at or before UTC instant；
- list branch commits in `(start, end]`；
- read path bytes at fixed Revision；
- resolve copy boundary。

可让现有 CLI Provider 实现一个附加的 History Protocol，保持 M2 的 `info/list/cat` 行为不变。

### 6.2 必须修复的风险

- `/branches/foo` 不能匹配 `/branches/foobar`；
- 一个 Revision 同时修改目标分支和其他路径时只保留目标分支；
- URL 编码、大小写和路径分隔符按 SVN 规范处理；
- 日期 Revision 的仓库全局解析不能被误当作目标分支发生了提交；
- 以 XML commit date 做最终 `(start, end]` 过滤；
- 分支复制前历史被 `--stop-on-copy` 截断；
- 分支在区间边界不存在时返回稳定业务错误。

## 7. 净值计算与字段归因

### 7.1 最终净值

1. 将区间起点和截止点分别解析为该分支在时间点有效的冻结 Revision；
2. 在两个 Revision 读取 Table Excel 清单和同侧 TableCsv；
3. 使用基线与截止清单的并集确定受影响工作簿和 Sheet；
4. 使用 `parse_table_csv` 和 `diff_table_csv` 计算最终净值；
5. 保留行新增、行删除、字段变化和字段定义变化；
6. 最终状态 `unchanged` 时仍生成规范空报告。

不得通过原始文本 diff 或 Excel 单元格 diff 替代业务语义。

### 7.2 归因事件账本

1. 读取区间内目标分支提交，按 Revision 升序回放；
2. 只物化与候选 Table/TableCsv 配对相关的变更；
3. 对每次提交计算前后业务语义事件；
4. 使用 `(workbook, sheet, row_key, field, event_type)` 作为归因身份；
5. 为最终净差异选择最后一次形成截止状态的事件；
6. 行新增保留创建事件并允许字段拥有后续最后修改事件；
7. 行删除使用删除事件；
8. 回放与最终净值无法可靠连接时输出未知归因并降级为 `partial`。

性能优化必须建立在正确结果之后。允许缓存固定 Revision 内容，但缓存键必须包含仓库身份、规范路径和 Revision，继续使用全局可再生 SVN 缓存治理规则。

## 8. 报告发布与保留

- `m3.monitor-report.v1` 是唯一业务结果；
- HTML 由严格契约渲染，所有业务文本和嵌入 JSON 必须防止 HTML/script 注入；
- 在目标目录内写临时文件、flush 后使用 `os.replace` 原子发布；
- 历史文件成功后再更新 `latest.html`；
- 发布失败保留旧 latest，并清理受管临时文件；
- 完整规范结果可使用 gzip 产物保存，数据库只存摘要、引用和 SHA；
- 读取报告必须校验引用归属，不能接受客户端物理路径；
- 30 天后报告 API 返回过期语义，物理清理由 Runner 或 Web 启动维护执行；
- 过期清理只处理精确受管格式，不递归删除未知项，不跟随符号链接。

## 9. API 与页面

建议页面路由：

- `/monitor`：创建任务和模块概览；
- `/monitor/tasks`：当前任务列表、状态、筛选和操作；
- `/monitor/reports/{run_id}`：受控打开离线报告内容。

建议 API：

- `POST /api/monitor/tasks`；
- `GET /api/monitor/tasks`；
- `GET /api/monitor/tasks/{task_id}`；
- `PATCH /api/monitor/tasks/{task_id}`；
- `POST /api/monitor/tasks/{task_id}/pause`；
- `POST /api/monitor/tasks/{task_id}/resume`；
- `POST /api/monitor/tasks/{task_id}/end`；
- `POST /api/monitor/tasks/{task_id}/archive`；
- `POST /api/monitor/tasks/{task_id}/scheduler-sync`；
- `GET /api/monitor/tasks/{task_id}/runs`；
- `POST /api/monitor/runs/{run_id}/retry`；
- `GET /api/monitor/runs/{run_id}/report`。

所有变更命令使用 request ID 保证接口幂等。活动任务的彻底删除不进入第一批页面主操作；如实现，必须使用固定确认文本并验证任务已结束或归档。

左侧导航和页面视觉应沿用现有工作台，不重排已验收的版本对比页面。监控任务列表以扫描效率为主，不使用营销式卡片布局。

## 10. 分阶段实施

### Phase 0：契约和确定性样例

- 新增四个 M3 契约文档、Pydantic schema 和最小 JSON 示例；
- 建立包含跨分支混合提交、同字段多次修改、回退、行增删和解析失败的 Mock SVN 样例；
- 冻结时间边界、状态和公开错误码。

门禁：契约测试通过，示例可严格往返序列化，未知字段被拒绝。

### Phase 1：分支历史与净值归因引擎

- 实现 History Protocol、严格 changed path 过滤和日期边界；
- 实现最终净值计算与字段事件账本；
- 复用 M2 TableCsv 语义并隔离旧 M0 报告链路。

门禁：纯单元/集成测试覆盖回退归零、字段最后作者、混合分支 Revision 和复制边界。

### Phase 2：SQLite、边界链和独立 Runner

- 实现任务、边界、Run、attempt、租约和恢复；
- 实现每日、首次、调度修改、暂停、恢复、结束和补跑；
- Runner 在不启动 FastAPI 的测试中完成端到端报告。

门禁：重复启动幂等，失败不改变后续区间，30 天历史回溯上限有效。

### Phase 3：报告 HTML 和生命周期治理

- 实现规范 JSON、离线 HTML、筛选搜索、原子发布和 SHA；
- 实现 `latest.html`、partial/failed 行为和 30 天过期；
- 增加大值、特殊字符和注入防护测试。

门禁：断开 Web 后本地 HTML 可打开；失败不覆盖 latest；过期清理范围可证明隔离。

### Phase 4：Windows 计划任务适配

- 实现 Scheduler Gateway、真实 Windows 适配器和 Fake；
- 实现创建、修改、暂停、恢复、结束、登录补跑、漂移检测和自动重试；
- 实现不依赖 Web 或活动监控任务的每日维护唤醒，触发全部任务的 30 天报告清理；
- 提供隔离前缀的安装、卸载及诊断命令。

门禁：只对测试前缀系统任务操作；Web/FastAPI 关闭后可触发 Runner；仅剩 ended/archived 任务时过期报告仍能被维护唤醒清理；清理后无孤立测试任务。

### Phase 5：API、导航和任务页面

- 新增左侧“版本监控”；
- 实现新建页面、二级任务列表、状态、操作和报告入口；
- 展示调度异常、Runner 心跳、遗漏补跑和部分成功；
- 保持 URL 刷新恢复、ETag 或等价低成本刷新策略。

门禁：契约、桌面/移动浏览器、键盘、空态、错误态和无重叠验收通过。

### Phase 6：真实只读验收与交接

- 使用用户授权的固定分支和短时间窗口做只读试跑；
- 对照 SVN 日志抽样验证 Revision、作者和字段净值；
- 验证 Web 关闭、屏幕锁定、注销后登录补跑；
- 更新 `AGENTS.md`、文档导航、模块手册和运行手册。

门禁：不写 SVN、不污染 M2 数据、不删除主缓存或真实历史报告；证据和已知限制记录完整。

## 11. 测试矩阵

| 层级 | 必测内容 |
|---|---|
| 时间单元测试 | 左开右闭、首份、结束 partial、调度变更、暂停/恢复、跨日、30 天上限 |
| SVN 单元测试 | 全局 Revision 间隙、严格分支前缀、混合提交、copy boundary、日期时区、路径删除 |
| 语义单元测试 | 改回归零、字段最后作者、行增删、字段定义、scope/type/主键一致性 |
| Store 单元测试 | 唯一截止点、租约、崩溃恢复、attempt、过期、隔离删除 |
| Scheduler 单元测试 | generation、旧触发空跑、漂移、权限失败、参数注入、暂停和结束 |
| API 契约测试 | 严格模型、幂等、状态冲突、错误码、报告归属和过期 |
| Runner 集成测试 | Web 未启动、多个遗漏截止点、部分失败、自动/人工重试、latest 发布 |
| 浏览器验收 | 新建、任务列表、筛选、状态、操作、报告入口、桌面与移动视口 |
| Windows 隔离验收 | 测试前缀计划任务精确触发、登录补跑、关闭 Web、最终卸载清理 |

## 12. 执行工作树建议

当前 `codex/m3-version-monitoring-report` 同时作为需求基线和主控集成分支。计划批准并提交后：

1. 每个 Phase 从主控分支最新验收提交创建独立 Codex 工作树和阶段分支；
2. 阶段任务提交并清理工作区后，由主控按门禁检查差异和测试；
3. 验收通过后将阶段提交合回主控分支，并更新 `M3-VERSION-MONITORING-STATUS.md`；
4. 下一阶段只能从更新后的主控 HEAD 创建，不能从未验收的阶段分支继续；
5. 只读调研、测试设计和代码审查可使用子 Agent，正式代码修改保持在阶段 worktree；
6. 任何产品语义变化先回到主控任务更新 PRD，再允许阶段实现继续；
7. Windows 真实计划任务验收必须使用隔离名称前缀并在验收后清理。
