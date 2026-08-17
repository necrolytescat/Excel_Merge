# M4 表格计划对比工作手册

> 状态：阶段五完成，M4 已交付
> 更新日期：2026-08-17

## 1. 当前范围

M4 是版本比对上方的长期计划与多目标分支编排层。它复用 SVN 端点注册表、TABLE 路径发现、冻结 Revision、单工作簿执行器、`m2.diff.v1`、Mapper 和明细渲染器，但拥有独立计划、运行和矩阵契约。

当前已交付：

- 全站左侧 M4 独立导航；
- 计划列表、归档视图、新建、编辑和详情；
- 单基准分支 TABLE Excel 目录；
- 已登记端点与 SVN 目录候选合并发现，基准单选及目标多选均支持模糊搜索；
- 1～10 张表格及 1～4 个目标分支选择；
- 默认 HEAD 与本次历史 Revision 控件；
- SQLite 计划持久化、幂等和乐观锁。

- 固定 Revision 的独立 M4 运行状态机和重启恢复；
- 最多 40 个“表格 × 目标分支”执行项；
- 哈希短路、M2 单工作簿执行器和原始 `m2.diff.v1` 结果；
- 整次取消和沿用父运行 Revision 的失败项重试；
- 全部分支矩阵、目标分支页签和可恢复 URL 定位；
- 计划详情中的完整 M4 运行历史。
- 30 天明细物理清理、启动恢复、每小时低频扫描和稳定 410 过期语义；
- M4 数据目录与全局 SVN 缓存清理严格隔离。

## 2. 关键边界

- 工作簿路径必须相对基准分支 TABLE 根目录，使用 `/`，不得包含空路径或重复路径。
- 创建和更新时服务端重新读取基准分支 HEAD 目录验证路径；浏览器清单不是安全边界。
- 搜索匹配分支目录名、区域和轨道；未登记的 SVN 候选只在实际选作基准或目标时写入端点注册表。
- 更换基准分支不会尝试按同路径静默迁移表格。
- 计划定义不保存 Revision；页面中的 Revision 只用于紧接着启动的运行。
- 归档计划不可编辑；恢复后才能修改。
- `var/m4-diff-plan/` 是运行数据，不进入 Git。
- 默认路径下 M2 与 M4 继续通过 `WorkbookExecutionGate` 共用进程级上限；启用 `workbook_execution.four_way_enabled` 后改由共享 SQLite 持久 slot + 进程 Gate 双重协调。
- M4 运行结果只引用原始 `m2.diff.v1`，前端继续使用 `M2DiffMapper` 和 `ExcelDiffResultsBridge`。
- `diff_plan.detail_retention_days` 默认 30；`diff_plan.cleanup_interval_seconds` 默认 3600 秒且最小 60 秒。
- 应用启动立即执行恢复和到期清理，随后按周期扫描；单文件删除失败不会阻断调度，下轮继续重试。
- 到期清理只清空内部 `result_path` 并删除 gzip；`result_ref`、矩阵摘要、冻结 Revision、统计和重试关系长期保留。

## 3. 当前结构

- 契约：`app/schemas/diff_plan.py`
- 计划存储：`app/services/diff_plan_store.py`
- 计划与目录校验：`app/services/diff_plan_service.py`
- 运行存储：`app/services/diff_plan_run_store.py`
- 冻结与调度：`app/services/diff_plan_run_service.py`
- API：`app/api/diff_plan.py`
- 页面：`app/templates/diff_plan*.html`
- 前端：`app/static/diff_plan*.js`、`app/static/diff_plans.css`
- 运行契约：`docs/contracts/m4.diff-plan-run.v1.md`

## 4. 阶段交接

阶段记录位于 `docs/M4-STAGE-HANDOFF.md`。恢复工作时先读本手册和 `docs/contracts/m4.diff-plan.v1.md`，不得从历史 M2 阶段材料反推 M4 行为。

## 5. 运维检查

- 服务：`http://127.0.0.1:5566`，健康检查 `GET /api/health`。
- M4 默认数据：`var/m4-diff-plan/diff-plan.sqlite3` 与同目录 `results/`。
- 不手工删除 SQLite 行或单独清空 `results/`；保留期由运行存储统一治理。
- 日志不记录请求体、SVN URL、凭据、原始异常或结果路径；清理日志只记录文件数和字节数。
- 全局 SVN 缓存清理不得指向或覆盖 M4 数据目录。

## 6. 共享四路调度与恢复

`app/services/workbook_execution_scheduler.py` 是 M2/M4 共用的持久调度库。开关开启时默认全局 4 个 slot、单个 `m2:task_id` 或 `m4:run_id` 最多 4 个；调度按 flow 公平轮转，只有一个积压 flow 时可占满 4 路。

领取顺序固定为获得持久 slot -> 获得进程 Gate -> 原子 claim Item -> 执行；claim 失败立即释放 slot。M4 运行项继续使用 lease token 防迟到提交，最多恢复一次，取消后不再领取新项，结果按 ordinal 稳定返回。Runner 崩溃或租约过期只恢复对应 Item，不得重复提交已经成功的结果。

四路开关默认关闭。上线必须先通过双 M2、M2+M4、公平性、崩溃恢复、迟到提交、取消和稳定 ordinal 测试，再灰度开启。

## 7. 已知限制

- 明细过期后不支持重新获取旧内容；需要查看明细时应创建新运行。
- 不提供手工强制清理 API，避免扩大运维写权限；清理由服务启动和周期任务执行。
