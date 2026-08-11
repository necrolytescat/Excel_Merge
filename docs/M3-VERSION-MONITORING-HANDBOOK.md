# M3 版本监控维护、验收与排障手册

> 当前状态：M3 已交付并合入 `main`
> 更新日期：2026-08-11
> 适用范围：版本监控页面、API、SQLite、独立 Runner、Windows 计划任务、SVN 历史、最终净值归因、离线报告和 30 天治理

## 1. 文档用途

本手册是后续 Agent 修改、扩展、验收和排查“版本监控”的首要入口。它回答四类问题：

1. 功能由哪些模块组成，调用链和事实来源是什么；
2. 哪些产品、数据和安全规则不能被局部需求破坏；
3. 修改后应怎样自动化验收和真实环境验收；
4. 页面、调度、Runner、SVN、Diff、报告或存储异常时，应先检查什么。

本手册不是新的产品契约。文档发生冲突时，按以下优先级判断：

1. 当前自动化测试与有效代码；
2. `docs/contracts/m3.*` 严格契约；
3. `M3-VERSION-MONITORING-PRD.md` 的产品语义；
4. ADR-006、ADR-007 和版本对比模块的 Table/TableCsv 规则；
5. 本手册；
6. `M3-VERSION-MONITORING-IMPLEMENTATION.md` 和阶段状态记录。

修改前至少读取与需求直接相关的本手册章节和对应契约。涉及最终净值、TableCsv 解析或配对时，再读取 `VERSION-COMPARISON-HANDBOOK.md`；涉及共享 SVN 缓存或运维日志时，再读取 `HISTORY-TASKS-HANDBOOK.md`。

## 2. 功能目标与边界

版本监控服务 QA 每日快速回归。用户绑定一个固定 SVN 分支，系统在计划截止点生成自上一个逻辑边界以来仍然存在的业务最终净变化。

必须保持：

- Web 页面和 FastAPI 关闭后，Windows 计划任务仍能启动独立 Runner；
- SVN 全链路只读，不执行 checkout、update、commit、merge、copy 或写回；
- 只比较 Excel `main` 清单确认导出的 TableCsv 业务字段；
- 报告区间确定、左开右闭、可重跑、无重复、无遗漏；
- 最终修改人来自字段事件，不退化为文件最后提交人；
- SQLite 是任务、边界、Run、attempt 和调度同步状态的事实来源；
- M3 状态和报告独立于 M2 BatchStore、M2 报告和应用日志；
- 历史 JSON/HTML 保留 30 天，任务 `latest.html` 不因历史过期或后续失败丢失。

不属于本模块：

- 展示区间内每一次中间修改；
- 比较 Excel 样式、公式、宏、备注、未导出字段或 `scope=none`；
- 生成 Excel/PDF、发送邮件或 IM；
- 多节点调度、分布式 Worker、权限或配额；
- Excel Merge 或任何 SVN 写回；
- 让用户指定任意物理报告目录。

## 3. 总体架构

```text
浏览器
  /monitor、/monitor/tasks
      |
      v
FastAPI /api/monitor/*
  app/api/monitor.py
      |
      v
MonitorWebService
  幂等命令、查询、人工重试 outbox、报告受控读取
      |
      +--> MonitorTaskService + MonitorSchedule
      |      任务生命周期、逻辑边界和 Run 物化
      |
      +--> MonitorStore (SQLite, WAL)
      |      唯一事实来源
      |
      `--> MonitorSchedulerService
             Windows Task Scheduler 同步和漂移校验

Windows Task Scheduler
  ExcelMerge-M3-Monitor-<task_uuid>
      |
      v
python -m app.monitor_runner
      |
      +--> BranchHistoryService --> CLISVNProvider --> svn info/log/list/cat
      +--> SvnMonitorSnapshotReader
      |      workbook main manifest + TableCsv
      +--> MonitorDiffService
      +--> MonitorAttributionService
      `--> FileSystemMonitorReportPublisher
             history JSON/HTML + latest.html

ExcelMerge-M3-Maintenance
  每日 03:15 启动 --maintenance，独立执行 30 天报告清理
```

### 3.1 三条执行路径

Web 管理路径：

```text
页面命令 -> API 严格请求 -> MonitorWebService 幂等账本
  -> MonitorTaskService 事务更新
  -> MonitorSchedulerService 同步系统任务
  -> 返回当前任务事实
```

计划报告路径：

```text
Windows 触发 -> 独立 Runner 读取 SQLite
  -> 物化到期边界/Run -> 租约抢占
  -> 固定 Revision 快照 -> 最终净值 -> 提交回放归因
  -> 准备 publication -> 不可变 history -> latest -> 原子完成
```

人工重试路径：

```text
POST retry -> 持久化 retry outbox -> 返回 202
  -> 事件驱动 dispatcher -> 原 Run 新 attempt
  -> 复用原区间和 logical_cutoff_at
```

202 只表示重试意图已经持久化，不表示 OS 进程已启动。系统保证至少一次派发，业务 Run、attempt 和报告发布保证幂等，不承诺进程 exactly-once。

## 4. 代码导航

### 4.1 页面、API 与装配

| 文件 | 职责 |
|---|---|
| `app/main.py` | 装配 MonitorStore、任务服务、Scheduler、Runner 和报告发布器；注册页面、异常处理与启动恢复 |
| `app/api/monitor.py` | endpoint、task、run、retry、report API；ETag 和报告安全响应头 |
| `app/schemas/monitor.py` | 全部严格请求、任务、Run、报告、错误和状态契约 |
| `app/services/monitor_web_service.py` | API 编排、request ID 幂等、分页、重试 outbox、受控报告读取 |
| `app/services/monitor_api_contract.py` | 规范 JSON、游标与 ETag 辅助逻辑 |
| `app/templates/monitor.html` | 新建任务和最近任务概览 |
| `app/templates/monitor_tasks.html` | 二级任务列表、详情和操作对话框 |
| `app/static/monitor.js` | endpoint 加载、创建任务、概览自动刷新 |
| `app/static/monitor_tasks.js` | 筛选、URL 恢复、详情、生命周期、重试和报告入口 |
| `app/static/monitor_request.js` | request ID 复用、失败恢复和刷新控制 |
| `app/static/monitor.css` | 版本监控桌面/移动布局和状态视觉 |

`app/main.py` 只在 Windows、Provider 实现历史协议且配置可装配时创建真实 M3 Web 服务。无法装配时页面仍可能存在，但 API 返回服务不可用；不要把这个状态误判成路由缺失。

### 4.2 时间、状态与存储

| 文件 | 职责 |
|---|---|
| `app/services/monitor_schedule.py` | `Asia/Shanghai` 日界、30 天回溯、首次/每日/结束边界 |
| `app/services/monitor_task_service.py` | 创建、到期物化、修改、暂停、恢复、结束、归档和公开投影 |
| `app/services/monitor_store.py` | schema v6、WAL、事务、CAS、租约、attempt、publication、命令账本和 retry outbox |
| `app/services/windows_scheduler.py` | 计划任务结构、XML、真实/Fake Gateway、SID 等价、generation 同步与漂移检测 |
| `app/monitor_scheduler_cli.py` | 维护任务、单任务同步和隔离 Windows 验收入口 |

### 4.3 SVN、Diff 与归因

| 文件 | 职责 |
|---|---|
| `core/svn_history.py` | 固定分支身份、commit、changed path 和 History Protocol |
| `core/svn_provider.py` | CLI 历史实现；保持既有 M2 Provider 行为 |
| `app/services/branch_history_service.py` | 冻结身份复核、日期 Revision、分支提交和固定 Revision 读取 |
| `app/services/monitor_diff_service.py` | 两端快照读取、清单配对、最终净值和局部错误隔离 |
| `app/services/monitor_attribution_service.py` | Revision 升序事件账本和最终状态归因 |
| `core/workbook_manifest_parser.py` | Excel `main` 清单；openpyxl 失败时安全 OOXML fallback |
| `core/table_csv_parser.py` | TableCsv 字段、主键、scope 和类型解析 |
| `core/semantic_diff.py` | 版本对比与 M3 共用的业务语义 Diff |

### 4.4 Runner 与报告

| 文件 | 职责 |
|---|---|
| `app/monitor_runner.py` | 无 FastAPI 依赖的 Runner、租约、错误分类、重试和维护 CLI |
| `app/services/monitor_report_service.py` | 唯一报告契约、稳定排序、统计、离线 HTML 和 SHA |
| `app/services/monitor_report_template.py` | 离线报告阅读工作台的 HTML、CSS 和安全 DOM 交互模板 |
| `app/services/monitor_report_artifacts.py` | 不可变 history、任务锁、原子 latest、归属校验和 30 天清理 |

## 5. 配置、运行与物理数据

### 5.1 配置前提

真实运行读取 `config/settings.json`，至少需要：

- `svn.provider=cli` 且本机 SVN CLI 可用；
- 已启用的 `svn.endpoint_registry`；
- 可装配的 `dataset_layout.workbook_source`、`csv_export` 和 `manifest`；
- 可选 `monitor.database_path`。

环境变量 `EXCEL_MERGE_MONITOR_DB` 优先覆盖数据库位置。默认数据库是：

```text
var/m3-monitor/monitor.sqlite3
```

默认报告根目录与数据库同级：

```text
var/m3-monitor/reports/<task_id>/
|-- latest.html
`-- history/
    |-- <logical-cutoff>.json
    `-- <logical-cutoff>.html
```

这些是本机业务数据，不进入 Git。不要为了让测试收集通过而提交真实 `config/settings.json`，也不要在错误输出中打印 URL、用户名、凭据或配置全文。

### 5.2 启动方式

```powershell
py -3 -m app.main
```

默认地址是 `http://127.0.0.1:5566/monitor`。端口来自 `web.port`，默认 5566；不要使用历史示例中的 8000 判断 M3 是否启动。

独立 Runner 正式命令由 Windows 任务生成，核心形式为：

```powershell
py -3 -m app.monitor_runner --task-id <uuid> --generation <n> --database <absolute-db> --scheduler-managed
```

人工/自动重试复用 Run：

```powershell
py -3 -m app.monitor_runner --run-id <uuid> --database <absolute-db>
py -3 -m app.monitor_runner --run-id <uuid> --automatic-retry --database <absolute-db>
```

保留治理：

```powershell
py -3 -m app.monitor_runner --maintenance --database <absolute-db>
```

CLI 退出码：0 表示成功、无事可做或计划任务管理下的确定性业务失败；75 表示临时失败，允许 Windows 重启；1 表示非计划任务管理下的确定性失败。

## 6. 时间、边界与生命周期

### 6.1 时间规则

- 所有权威瞬时点以 UTC 存储和传输；
- 每日触发时间是 `Asia/Shanghai` 的本地墙上时间，精确到秒；
- 正常报告区间是 `(上一个逻辑边界, 本次逻辑截止点]`；
- 首份报告是 `(effective_at, first_cutoff]`；
- Runner 实际启动或完成时间不能改变区间；
- 正好发生在截止点的提交属于本报告；
- 无变化也必须生成成功报告；
- 创建时 effective_at 最多回溯 30 天，且不能早于分支复制边界。

失败不会吞并下一天：8 月 10 日失败后，8 月 11 日仍只覆盖 10 日截止点到 11 日截止点。重试 10 日报告仍使用原 Run 和原区间。

### 6.2 边界类型

| 边界 | 是否生成 Run | 含义 |
|---|---:|---|
| `start` | 否 | 初始起点 |
| `scheduled` | 是 | 每日计划截止点 |
| `pause` | 是 | 暂停前最后一个不足日区间 |
| `resume` | 否 | 恢复后的新起点，暂停期间不补算 |
| `end` | 是 | 最终截止点 |

结束时间与计划点相同时只允许一份 Run。修改触发时间不重写既有边界；下一份报告从已有最后边界接到新计划的首个有效点。

### 6.3 状态不可混用

任务业务状态：

- `syncing`：业务配置已写入，系统任务待同步；
- `active`：业务有效且 Scheduler 为 `enabled + synced`；
- `paused`：主动暂停，暂停期间不补跑；
- `scheduler_error`：配置仍在，但系统任务同步或校验失败；
- `ended`：最终截止点已进入执行链，不表示最终报告必然成功；
- `archived`：默认列表隐藏，任务只读。

调度同步状态独立为 `pending / synced / drifted / error / not_present`。页面显示“调度异常”时，不能只改业务状态来掩盖系统漂移。

Run 状态：`queued / running / succeeded / partial / failed`。只有 succeeded/partial 拥有 Revision、summary 和报告；failed 不得覆盖旧 latest。

## 7. SVN 固定分支规则

创建任务时冻结：endpoint ID、仓库 UUID、规范 URL、仓库相对路径、bound Revision 和 copy boundary。固定分支之后不可修改，更换分支必须创建新任务。

每次运行重新验证：

- 仓库 UUID 相同；
- canonical URL 相同；
- repository relative path 相同。

历史读取继续受创建时记录的 copy boundary 约束，不能越过分支复制边界追入源分支历史。

Revision 是仓库全局编号，不可能成为分支内连续编号。允许目标分支 Revision 有间隙，但报告和日志只能保留固定分支路径实际相关的提交。

History 实现必须保持：

- 路径按解码后的完整分段匹配，`foo` 不能匹配 `foobar`；
- 同一 Revision 混合修改多个分支时，只保留目标分支 changed paths；
- `svn log --stop-on-copy` 与 copy boundary 阻止追入源分支历史；
- XML commit date 再做一次精确 `(start, end]` 过滤；
- 日期 Revision 只代表该时刻仓库有效状态，不代表目标分支在该 Revision 提交；
- 固定 Revision 内容读取使用 peg-safe 路径；
- M3 不复用旧 `fetch_revisions`，也不改变 M2 的 info/list/cat 行为。

排障和验收允许的 SVN 操作仅为 `info / log / list / cat` 等只读查询。禁止为了制造样例执行 SVN commit、copy、merge、update 或写回。

## 8. 最终净值、清单与归因

### 8.1 快照覆盖由 Excel main 决定

每个 Revision 必须先解析 Table Excel 的 `main` 清单，再按 `sheetName / tbxName / isExport` 找同 Revision 的 TableCsv。CSV 文件已经存在，不代表它已经进入业务快照；只有 `main` 清单确认导出后才参与比较。

这是查“变化数不符合直觉”时的首要检查点。M3 真实验收曾出现：CSV 两端内容相同，但截止 Revision 才被 `main` 清单纳入，因此正确结果是整张 Sheet 的字段新增和行新增，而不是 0 条变化。

不得绕过 manifest，直接把目录中的全部 CSV 强行加入两端快照。这样会漏报或改变变化类型。

### 8.2 业务 Diff

- 值只来自可靠 TableCsv；
- 主键优先 `Id/id` 大小写不敏感唯一匹配，再使用冻结的第一列兜底；
- 字段身份是稳定 `field_name`，display name 只是元数据；
- `scope=none` 不比较；
- 整数、小数、布尔、日期和时间沿用版本对比规范化；
- 不使用行号、内容哈希或模糊相似度作为业务身份；
- 单工作簿或 Sheet 解析失败局部隔离，其他可靠结果仍可生成 partial。

变化类型：

| 类型 | row_key | 值形态 |
|---|---|---|
| `field_modified` | 非空业务主键 | 两侧 scalar |
| `row_added` | 非空业务主键 | target 完整 row_values |
| `row_deleted` | 非空业务主键 | source 完整 row_values |
| `field_added` | `null` | target 字段定义 |
| `field_removed` | `null` | source 字段定义 |
| `field_definition_modified` | `null` | 两侧字段定义 |

三类结构变化是 Sheet 级事件，不能使用哨兵主键，也不计入 `changed_row_count`。

### 8.3 最终净值与归因

先比较起止快照，再只为最终仍存在的变化回放区间提交：

- `100 -> 120 -> 110` 报告 `100 -> 110`，作者是形成 110 的提交人；
- `100 -> 120 -> 100` 不进入报告；
- 文件后续被其他人修改，但目标字段未变，不能覆盖字段作者；
- 新增行使用创建事件，后续字段可有自己的最后修改事件；
- 删除行使用删除事件；
- 结构变化使用最后形成该结构状态的事件。

归因状态：

- `attributed`：作者、Revision 和时间完整；
- `unknown_author`：SVN author 缺失，显示“未知”，但保留 Revision/时间；
- `unresolved`：无法可靠连接，作者显示“未知”，Revision/时间为空，报告必须 partial。

## 9. SQLite、幂等、租约与恢复

当前 schema 版本为 6，连接启用外键、30 秒 busy timeout 和 WAL。主要表：

| 表 | 事实 |
|---|---|
| `monitor_tasks` | 固定分支、业务生命周期、schedule generation、调度期望/事实和心跳 |
| `monitor_boundaries` | 不可反推替代的完整逻辑边界链 |
| `monitor_runs` | 每个逻辑截止点唯一 Run、租约、Revision、summary 和报告引用 |
| `monitor_run_attempts` | scheduled/automatic_retry/manual_retry 的每次尝试 |
| `monitor_run_publications` | prepared/activated 两阶段发布事实 |
| `monitor_commands` | request ID 幂等命令账本 |
| `monitor_retry_outbox` | 人工重试的持久化派发意图 |
| `monitor_schema_migrations` | 已应用 schema 版本 |

关键唯一性：

- `(task_id, boundary_at, boundary_type)` 唯一；
- `(task_id, logical_cutoff_at)` 唯一；
- 每个 boundary 只对应一个 Run；
- request ID 数据库全局唯一；
- publication 每个 Run 唯一。

Runner 使用 5 分钟租约并在阻塞 SVN/文件工作期间续期。重复 Windows 触发、旧 generation 或并发重试只能有一个执行者。不要通过直接 UPDATE SQLite “解锁”；应先确认进程是否存在、租约是否过期，再使用正式恢复路径或重试。

数据库可以只读诊断，但不是人工修数据入口。任何 migration 必须追加版本、事务执行、覆盖旧库升级和新库创建；不得原地改旧 migration 语义。

## 10. Windows 计划任务

正式任务名：

- `ExcelMerge-M3-Monitor-<task_uuid>`；
- `ExcelMerge-M3-Maintenance`。

每个活动任务定义冻结为：

- 当前 Python 解释器绝对路径；
- 固定工作目录；
- 参数只含模块、Task ID、generation、数据库绝对路径和 scheduler-managed；
- 每日触发器 1 个；
- 当前用户登录触发器 1 个，用于遗漏补跑；
- 配置 end_at 时有精确结束触发器 1 个；
- `StartWhenAvailable=true`；
- 临时失败每 10 分钟重启，最多 3 次；
- 最长执行 6 小时；
- `IgnoreNew` 防止重复实例；
- `InteractiveToken`、`LeastPrivilege`、当前登录用户；
- 不保存 Windows 密码。

维护任务默认每天 03:15 运行，不依赖 Web 或活动监控任务。禁止改回每分钟轮询。

Scheduler 校验不仅看“任务存在”，还检查 enabled、用户/SID、解释器、参数、工作目录、触发器数量与时间、登录/结束触发、RunLevel、Context、重试、最长时间和重复实例策略。Windows XML 可能省略默认 `Enabled=true` 或 `RunLevel=LeastPrivilege`，解析器按系统默认规范化；不能因此放宽显式禁用、其他用户或额外触发器。

generation 防止旧同步覆盖新配置。旧系统任务触发 Runner 后，Runner 必须重新读取 SQLite；任务已暂停、结束、归档、generation 过期或没有到期边界时应正常空跑退出。

## 11. 报告、latest 与保留治理

`m3.monitor-report.v1` 是唯一业务 JSON，离线 HTML 只消费该契约。规范 JSON 使用 UTF-8、两空格缩进、稳定排序和结尾换行；SHA-256 基于规范字节。

发布顺序：

1. 构建严格报告和稳定统计；
2. 在数据库准备 publication manifest；
3. 同目录临时文件 + flush + `os.replace` 发布不可变 history JSON/HTML；
4. 在任务级锁内原子替换 `latest.html`；
5. 数据库原子激活 publication 和 Run。

成功、无变化和 partial 可以更新 latest；完全 failed 或发布失败不得覆盖旧 latest。读取历史报告时重新校验 task/run/publication 归属和 SHA，不接受客户端路径或任意 report_ref。

完整 history JSON/HTML 自 generated_at 起保留 30 天。过期后：

- 历史 Run API 返回 410；
- 轻量 Run 摘要、状态、计数和哈希保留；
- `latest.html` 保留；
- 清理只删除可验证的成对受管文件；
- 不递归删除未知文件，不跟随符号链接，不触碰 M2、缓存或其他任务。

### 11.1 报告阅读工作台

离线 HTML 按“工作簿 -> Sheet -> 主键 ID -> 字段”组织最终净变化，目标是让 QA 先定位受影响范围，再按原表列式结构逐行回归：

- 左侧导航列出契约中可以识别的有变化或公开错误工作簿；顶部页签列出当前工作簿中有变化或错误的 Sheet；
- `m3.monitor-report.v1` 只有 `workbook_count` 总数，不包含无变化工作簿名称。页面只能显示未列出工作簿的数量，不能猜测或伪造名称；若未来必须逐个显示，需先补充业务事实并变更契约；
- 主键 ID 一行、字段一列；字段修改在对应单元格内显示前值和后值，不为每个字段额外展开一行；
- 网格字段来自本报告变化项和行增删的 `row_values`，只代表当前报告可恢复的字段集合，不保证是完整原表字段清单或原始列顺序；
- 新增行使用绿色；删除行和删除字段整行或整列使用红色；字段定义变化使用黄色；`field_added` 不单独生成结构列，新增行中的实际字段值仍按普通 Excel 列展示；
- 字段新增、删除和定义变化仍是 `row_key=null` 的 Sheet 级事件，不得伪造主键或按业务行展开；其中字段删除和定义变化在表头表达，字段新增不单独展示；
- 点击变化单元格或结构变化表头后，右侧归因栏只显示最终修改人、Revision 和修改时间，继续使用契约中的字段级最终归因；
- 变化网格必须支持鼠标按住拖拽上下左右滚动、触控原生双向滚动和键盘聚焦滚动；滚动或拖拽不能误触归因选择；
- 搜索和最终修改人筛选只改变展示，不改变报告 JSON、摘要统计或最终净值语义。

读取旧历史 HTML 时，服务可以根据其中内嵌的原始报告 JSON 在内存中渲染当前工作台，但不得改写历史 HTML/JSON、SHA、publication 或 `latest.html`。新模板仍只消费 `m3.monitor-report.v1`，不增加 SVN 读取、额外 Diff 或第二套业务契约。

## 12. Web、API 与前端规则

页面：

- `/monitor`：创建任务和最近任务；
- `/monitor/tasks`：任务列表、筛选、详情、Run 和操作；
- `/monitor/reports/{run_id}`：307 到受控报告 API。

查询 API：

- `GET /api/monitor/endpoint-options`；
- `GET /api/monitor/tasks`；
- `GET /api/monitor/tasks/{task_id}`；
- `GET /api/monitor/tasks/{task_id}/runs`；
- `GET /api/monitor/runs/{run_id}/report`；
- `GET /api/monitor/tasks/{task_id}/latest-report`。

写 API：

- `POST /api/monitor/tasks`；
- `PATCH /api/monitor/tasks/{task_id}`；
- `POST pause/resume/end/archive/scheduler-sync`；
- `POST /api/monitor/runs/{run_id}/retry`。

所有 POST/PATCH body 都携带 UUID request ID 并严格拒绝未知字段。相同 method、target 和规范 payload 重放首次结果；同 ID 用于不同请求返回 409。创建只接收 enabled endpoint ID；PATCH 只能完整替换 daily trigger 和 end_at，不能修改固定分支、effective_at 或时区。

列表和详情使用 SQL 分页/批量摘要，禁止回退为全表扫描或 N+1。普通列表刷新不逐任务调用 schtasks；只有 scheduler-sync 显式检查并修复系统任务。

JSON 查询使用强 ETag，列表 ETag 排除 `as_of`；报告以发布 HTML SHA 作为 ETag。报告响应设置 nosniff、禁止 framing、no-referrer 和严格 CSP。

前端保持：

- 筛选和选中任务写入 URL，刷新可恢复；
- 自动刷新失败显示陈旧状态，不清空最后成功结果；
- 新筛选取消旧请求，旧响应不能覆盖新状态；
- 网络结果未知时复用原 request ID；
- archived 只读，failed Run 才显示人工重试；
- latest report 与 latest Run 分开显示；
- 动态业务文本使用安全 DOM API，不拼接不可信 HTML。

## 13. 自动化验收

### 13.1 分层命令

SVN 历史与最终净值：

```powershell
py -3 -m pytest tests/unit/test_svn_history.py tests/unit/test_monitor_diff_attribution.py tests/integration/test_monitor_phase1_diff.py
```

时间、Store 与 Runner：

```powershell
py -3 -m pytest tests/unit/test_monitor_schedule_store.py tests/integration/test_monitor_phase2_runner.py
```

报告生命周期：

```powershell
py -3 -m pytest tests/unit/test_monitor_report_service.py
```

Windows 调度：

```powershell
py -3 -m pytest tests/unit/test_windows_scheduler.py
```

契约、API 与页面：

```powershell
py -3 -m pytest tests/contract/test_monitor_contracts.py tests/contract/test_monitor_api.py tests/contract/test_monitor_page.py
```

最低收尾检查：

```powershell
py -3 -m py_compile app/monitor_runner.py app/monitor_scheduler_cli.py app/api/monitor.py app/services/*.py core/svn_history.py core/svn_provider.py
git diff --check
```

PowerShell 不一定展开 `app/services/*.py` 给 Python；实际执行时应使用仓库既有测试命令或显式文件列表，不要因 shell 差异误报语法失败。

### 13.2 修改类型与必测范围

| 修改 | 必测 |
|---|---|
| 时间/生命周期 | schedule + store + Runner 集成 + API 状态冲突 |
| SVN 路径/日期/复制边界 | svn history + Phase 1 集成 + 旧 Provider 回归 |
| manifest/TableCsv/Diff | parser + semantic diff + Phase 1 + 版本对比回归 |
| 归因 | 多提交、回退归零、行增删、结构变化、unknown/unresolved |
| SQLite/migration/租约 | 新库、旧库升级、CAS、并发 claim、崩溃恢复 |
| Scheduler/XML | Fake、XML parser、SID、漂移、真实隔离任务 |
| 报告/保留 | 契约往返、注入、SHA、原子发布、latest、过期隔离 |
| 报告工作台 | 工作簿/Sheet 切换、主键行字段列、结构变化、归因栏、双向拖拽、桌面/移动 |
| API/前端 | 严格请求、幂等、ETag、分页、断网 request ID、桌面/移动 |

不得只跑新增 happy path。共享 parser、Provider 或 semantic diff 发生变化时，必须补版本对比 M2 回归。

## 14. 真实环境验收

### 14.1 前置条件

- 用户明确授权固定 endpoint 和只读 SVN 访问；
- 本机 SVN CLI/凭据已正常配置；
- 选择短窗口和可解释的真实提交；
- 不打印 endpoint URL 或凭据；
- 不创建 SVN 写操作；
- Windows 真实验收任务必须可识别并在结束后确认清理；
- 保持当前用户登录；注销/锁屏验收需要用户知情。

### 14.2 最终净值验收步骤

1. 只读查询目标分支近期 `svn log`，找实际修改 TableCsv 或 Table Excel `main` 清单的 Revision；
2. 把 effective_at 放在目标提交之前，并解析预期 start Revision；
3. 触发点至少留出 10 分钟，避免配置/同步尚未完成；
4. 先用正式 manifest + TableCsv + Diff 计算预期变化，不得绕过 manifest；
5. 创建一次性任务，确认业务状态 active、调度 synced、next cutoff 正确；
6. 触发前记录 Windows XML 的时间、Action、generation 和 StartWhenAvailable；
7. 报告完成后核对 start/end Revision、workbook/sheet/row/field、前后值、作者、Revision 和变化类型；
8. 对至少一条字段修改、一条行变化和一条结构变化做固定 Revision 文件抽样；
9. 确认 `errors=0`，或逐条解释 partial 的公开覆盖缺口；
10. 验收后确认临时任务结束，Windows 计划任务已删除或恢复到业务期望。

不能只确认“报告 HTTP 200”。必须预先得到可验证答案，再比较报告。

### 14.3 Web 关闭独立调度验收

1. 任务达到 `active + synced` 后，确认监听进程确为 `python -m app.main`；
2. 只停止 Web 进程，确认 5566 端口已关闭；
3. 不手工执行 Runner，不调用 retry；
4. 跨过计划触发点后，以 SQLite `mode=ro` 检查 heartbeat、Run 和 attempt；
5. 观察租约是否续期，不能把耗时运行误判为卡死；
6. 等待 succeeded/partial/failed 明确终态；
7. 确认 Runner 退出和一次性 Windows 任务清理；
8. 重启 Web，验证 task、Run、latest report 和历史报告均可读取。

通过标准：Web 端口关闭期间，Windows 任务自行产生 heartbeat 和 Run，并完成报告或可解释失败。

### 14.4 浏览器验收

- `/monitor`：端点、时间校验、创建成功/错误、最近任务；
- `/monitor/tasks`：状态筛选、关键词、分页、URL 恢复、详情；
- 生命周期：暂停、恢复、修改、结束、归档、scheduler-sync；
- Run：queued/running/succeeded/partial/failed、attempt 和人工重试；
- latest 旧报告不能被新 failed Run 覆盖；
- 报告筛选、搜索、特殊字符和离线打开；
- 报告工作簿和 Sheet 切换、主键一行字段一列、前后值显示与空状态；
- 新增行为绿色，删除行和删除字段整行/整列为红色，字段定义变化为黄色；新增字段不单独生成结构列；
- 点击变化单元格或结构表头后，归因栏的最终修改人、Revision、修改时间与报告 JSON 一致；
- 变化网格可用鼠标拖拽、触控和键盘进行上下左右滚动，拖拽不误触单元格；
- 360px 移动视口和桌面视口无重叠、溢出或不可操作控件；
- 键盘焦点、对话框关闭和错误提示可用。

### 14.5 完成门禁

宣布完成前至少具备：

- 相关自动化全绿；
- 真实固定分支报告抽样正确；
- Web 关闭独立触发成功；
- 报告发布、latest 和 30 天治理证据；
- Windows 临时任务无遗留；
- 工作区 clean，文档和契约同步；
- 未执行 SVN 写操作，未泄露内部信息；
- 未完成的锁屏、注销、移动端或环境验收明确记录为限制。

## 15. 只读诊断方法

以下命令仅用于定位。执行前确认数据库路径和 Task/Run ID，不输出配置全文。

### 15.1 Web 与端口

```powershell
Invoke-RestMethod http://127.0.0.1:5566/api/health
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 5566 -State Listen
```

`/monitor` 返回 404 通常是旧分支/旧进程；API 返回 503 通常是 M3 服务装配失败。先核对进程命令行、当前 Git HEAD、Windows、Provider 历史能力和配置，不要改路由掩盖装配失败。

### 15.2 SQLite 只读检查

必须使用 `mode=ro`，示例：

```powershell
py -3 -c "import sqlite3; db=sqlite3.connect('file:var/m3-monitor/monitor.sqlite3?mode=ro',uri=True); print(db.execute('select task_id,lifecycle,scheduler_sync_status,last_runner_heartbeat_at from monitor_tasks').fetchall())"
```

常用检查顺序：

1. `monitor_tasks`：lifecycle、generation、desired state、sync status、heartbeat；
2. `monitor_boundaries`：最后边界、类型、generation；
3. `monitor_runs`：区间、状态、attempt_count、租约、Revision、report_ref；
4. `monitor_run_attempts`：trigger、状态、公开错误；
5. `monitor_run_publications`：prepared/activated；
6. `monitor_commands` 和 `monitor_retry_outbox`：幂等命令或重试是否卡住。

不要在排障手册或聊天中输出 `canonical_url` 字段。

### 15.3 Windows 计划任务

只读查询：

```powershell
schtasks.exe /Query /TN ExcelMerge-M3-Monitor-<task_uuid> /XML
schtasks.exe /Query /TN ExcelMerge-M3-Maintenance /XML
```

重点核对：任务是否存在、Enabled、当前用户、Action、工作目录、daily/login/end 触发器、时间、generation、StartWhenAvailable、重试和 MultipleInstancesPolicy。

普通故障定位不得先删任务重建。先用页面/API 的 scheduler-sync，让服务按数据库期望修复；只有隔离测试任务或明确授权的无效任务才能删除。

### 15.4 报告文件

核对同一 cutoff 的 JSON/HTML 是否成对存在，数据库 SHA 是否匹配，latest 是否指向已激活的最新成功/partial 报告。不要通过直接复制 history 覆盖 latest；这会绕过锁、SHA、归属和 publication 状态。

## 16. 常见故障定位

### 16.1 页面存在，但创建或列表显示服务不可用

检查：

1. 是否运行 Windows；
2. `config/settings.json` 是否存在且结构有效；
3. Provider 是否为 CLI 且支持 History Protocol；
4. dataset_layout 是否完整；
5. 数据库目录是否可创建/打开；
6. `app.main` 启动时是否把 `monitor_web_service` 留为 None。

不要把原始配置异常或 SVN stderr 返回前端。

### 16.2 新任务显示“调度异常”

先区分 `sync_status=error` 与 `drifted`：

- error：创建/更新/查询 schtasks 失败，检查权限、System32 工具、当前用户和路径；
- drifted：系统任务存在但定义不一致，检查 drift_fields 对应的触发器、Action、SID、RunLevel 等。

使用 scheduler-sync 修复。不能直接把数据库 status 改成 active。

### 16.3 active 任务到点没有 Run

按层检查：

1. next logical cutoff 是否真的到期；
2. Windows 任务是否 synced、Enabled、时间/时区正确；
3. Action 中 Task ID、generation、DB 路径和工作目录是否正确；
4. 用户是否登录，或是否应由登录触发补跑；
5. Runner heartbeat 是否更新；
6. boundary 是否已存在但 Run 未物化；
7. 旧 generation 是否正常空跑。

不要手工插入 Run。边界与 Run 必须由服务事务物化。

### 16.4 Run 长时间 running

先看进程、CPU、SVN 子进程和 `lease_expires_at`：

- 租约持续续期且 CPU/SVN 有活动：仍在执行，真实 197 工作簿可能耗时数十分钟；
- 进程消失且租约已过期：下一次正式 claim 可恢复；
- 租约未续期但进程仍在：检查数据库锁、keepalive 线程和系统资源；
- 超过 Windows 6 小时上限：调查数据规模或死锁，不能简单放宽上限掩盖问题。

不要停止仍在正常续租的 Runner，也不要直接清空 lease token。

### 16.5 Run failed 或 partial

先按 `errors[].stage` 定位：

| stage | 首查模块 |
|---|---|
| `scheduler` | Windows Scheduler 同步/漂移 |
| `branch_identity` | endpoint、UUID、URL、分支相对路径 |
| `history` | 日期 Revision、log XML、copy boundary、网络 |
| `snapshot` | 固定 Revision list/cat 与目录定位 |
| `manifest_parse` | Excel main、openpyxl、OOXML fallback |
| `csv_parse` | TableCsv 结构、主键、编码 |
| `diff` | semantic diff 和类型规范化 |
| `attribution` | 事件账本与最终变化连接 |
| `report_publish` | 文件占用、SHA、不可变冲突、目录安全 |

retryable 错误才应自动重试。认证、绑定、配置和确定性解析错误不能靠重复运行解决。

### 16.6 报告变化数“不对”

按以下顺序核对：

1. interval 与 start/end Revision 是否正确；
2. 两端 Excel `main` 清单分别包含哪些 Sheet；
3. 清单条目对应的 TableCsv 是否同 Revision、唯一匹配；
4. 字段 scope、主键和类型规范化；
5. 变化类型是否因整 Sheet 新增/删除而改变；
6. `changed_row_count` 是否正确排除了 `row_key=null` 的结构变化；
7. 报告 summary 是否可从 `changes[]` 反算。

不要先对 CSV 做文本 diff。CSV 存在但未进入 manifest 是最常见的误判来源之一。

### 16.7 作者或 Revision 不对

检查目标字段的事件链，不要只看文件 log：

- commits 是否按 Revision 升序；
- changed paths 是否严格限制固定分支；
- 每个事件前后快照是否正确；
- 最后一次事件是否真正形成截止值；
- 后续只改文件其他字段的提交是否错误覆盖作者；
- manifest 新纳入 Sheet 时，归因应落在形成导出结构的工作簿提交。

无法可靠连接必须 unresolved + partial，不能猜作者。

### 16.8 latest 没更新或打开旧报告

检查 Run 是否 succeeded/partial、publication 是否 activated、history SHA 是否匹配、latest 锁和 `os.replace` 是否成功。新 Run failed 时保留旧 latest 是正确行为，不是缺陷。

### 16.9 报告 404 或 410

- 404：核对 task/run/publication 归属、文件是否存在、SHA 是否匹配；
- 410：历史报告已过 30 天，是预期语义；
- task latest 在任务未彻底删除前应继续可读，若 latest 也失效需调查治理范围。

不要接受客户端物理路径绕过 API。

### 16.10 人工重试一直未执行

检查：

- 原 Run 是否确为 failed，任务是否未归档；
- `monitor_commands` 是否 complete/pending；
- `monitor_retry_outbox` 是否 pending/dispatching/dispatched；
- Web 启动恢复是否执行；
- dispatcher 是否按 next wakeup 唤醒；
- 同 request ID 是否发生幂等冲突。

不允许恢复每分钟扫描；dispatcher 必须按事件或下一到期时间唤醒。

### 16.11 30 天历史没有清理

检查维护任务是否存在且 synced、03:15 是否触发、`--maintenance` 使用的 DB 是否正确，以及目标文件是否为可验证成对受管 history。未知文件、符号链接、latest 或缺配对文件不应被激进删除。

## 17. 公开错误码

API 错误主要包括：

- `MONITOR_INVALID_REQUEST / INVALID_CURSOR`；
- `MONITOR_ENDPOINT_NOT_FOUND / ENDPOINT_DISABLED`；
- `MONITOR_BRANCH_CONFIGURATION_INVALID`；
- `MONITOR_DATASET_CONFIGURATION_INVALID`；
- `MONITOR_TASK_NOT_FOUND / RUN_NOT_FOUND / REPORT_NOT_FOUND`；
- `MONITOR_REPORT_EXPIRED`；
- `MONITOR_STATE_CONFLICT / IDEMPOTENCY_CONFLICT`；
- `MONITOR_SERVICE_UNAVAILABLE / API_INTERNAL_ERROR`。

Run/报告公开错误：

- `MONITOR_SVN_TIMEOUT / SVN_AUTH_FAILED`；
- `MONITOR_BRANCH_BINDING_INVALID`；
- `MONITOR_CONFIGURATION_INVALID / PARSE_FAILED`；
- `MONITOR_ATTRIBUTION_INCOMPLETE`；
- `MONITOR_REPORT_PUBLISH_FAILED`；
- `MONITOR_SCHEDULER_SYNC_FAILED`；
- `MONITOR_INTERNAL_ERROR`。

公开错误只能包含 code、stage、脱敏 message、retryable 和可选 workbook/sheet。禁止 details、物理路径、URL、stderr、异常或堆栈。

## 18. 新增或修改功能的路径

### 18.1 改产品语义

先更新 PRD，再同步：schema、契约 Markdown/示例、核心服务、报告、前端、测试和本手册。不能只改 HTML 展示来改变业务含义。

### 18.2 新增变化类型或报告字段

至少修改：

1. `app/schemas/monitor.py`；
2. `docs/contracts/m3.monitor-report.v1.*`；
3. Diff/Attribution；
4. report stable sort、summary 反算和 HTML；
5. API/前端消费；
6. Mock fixture、契约、单元、集成和注入测试。

先明确 row_key、统计、归因、partial 和向后兼容语义。

### 18.3 改时间或生命周期

同时审查 schedule、task service、store 事务、Scheduler generation、Runner 旧触发、API 状态冲突、页面操作和边界链测试。不得从当前配置反推或重写历史边界。

### 18.4 改 Windows 调度

先改 ExpectedSchedulerTask 和验证器，再改 XML 生成/解析、Fake、真实 Gateway、同步服务和隔离验收。新字段必须既生成又验证；默认省略语义需要真实 Windows XML 证据。

### 18.5 改存储

追加新 migration，提高 schema version，并覆盖：空库、旧库升级、重复启动、事务失败和并发恢复。不要删除或改写既有 migration。

### 18.6 改页面

保持严格契约、request ID、ETag、URL 恢复、并发请求隔离、安全 DOM、移动布局和任务/调度双状态。禁止通过前端猜测或修补数据库事实。

### 18.7 性能优化

先保留确定性结果，再优化：

- 固定 Revision 缓存键必须含仓库身份、规范路径和 Revision；
- 列表保持 SQL 分页和批量摘要；
- 不以跳过 manifest、归因回放或覆盖校验换速度；
- 不把报告调度改为分钟轮询；
- 优化前后用真实工作簿数量记录耗时、内存、SVN 调用和结果哈希。

## 19. 安全与审查清单

提交前逐项确认：

- [ ] SVN 仍严格只读；
- [ ] 固定分支身份和 copy boundary 未放宽；
- [ ] 区间仍是计划时间的左开右闭；
- [ ] manifest 与 TableCsv 来自同一固定 Revision；
- [ ] 最终净值和字段归因未退化；
- [ ] 结构变化 row_key 仍为 null 且不计变化行；
- [ ] 任务、调度、Run 状态仍分层表达；
- [ ] request ID、CAS、generation、租约和 publication 幂等仍成立；
- [ ] failed 不覆盖 latest，partial 不伪装 succeeded；
- [ ] 30 天清理不删除 latest、未知文件、M2 或其他任务；
- [ ] API/报告不暴露 URL、凭据、物理路径、stderr 或堆栈；
- [ ] 共享 parser/Provider/Diff 改动已跑版本对比回归；
- [ ] 真实 Windows 验收无孤立测试任务；
- [ ] 文档、契约、示例和 AGENTS 路由同步。

## 20. 当前验收基线与已知限制

M3 完成时的真实证据记录在 `M3-VERSION-MONITORING-STATUS.md`：

- 固定分支真实只读报告成功；
- Web 关闭期间 Windows 独立 Runner 成功触发；
- `r26475 -> r26514` 比较 197 个工作簿，得到 116 条最终净变化、0 错误；
- Revision、作者、字段前后值和 manifest 新增 Sheet 已抽样复核；
- Runner 退出后一次性 Windows 任务正常清理；
- Web 重启后 latest 报告 HTTP 200。

当前非阻断限制：

- 锁屏和注销后登录补跑有自动化与任务结构覆盖，但完成验收时未实际注销用户会话；
- in-app 浏览器自动截图曾受 Windows `CreateProcessWithLogonW failed: 1385` 阻断；
- 真实大分支完整比较可能耗时二十分钟以上，诊断时应结合租约和进程活动判断。

后续优化不能删除这些限制记录，除非有新的真实证据替代。

## 21. 相关文档

- `docs/M3-VERSION-MONITORING-PRD.md`
- `docs/M3-VERSION-MONITORING-IMPLEMENTATION.md`
- `docs/M3-VERSION-MONITORING-STATUS.md`
- `docs/contracts/m3.monitor-api.v1.md`
- `docs/contracts/m3.monitor-task.v1.md`
- `docs/contracts/m3.monitor-task-list.v1.md`
- `docs/contracts/m3.monitor-run.v1.md`
- `docs/contracts/m3.monitor-run-list.v1.md`
- `docs/contracts/m3.monitor-report.v1.md`
- `docs/VERSION-COMPARISON-HANDBOOK.md`
- `docs/HISTORY-TASKS-HANDBOOK.md`
- `docs/adr/ADR-006-m1-head-freeze-table-excel.md`
- `docs/adr/ADR-007-m2-table-tablecsv-pairing.md`
