# M2-05 阶段 D2：批量 Diff 实施评审交接

> 状态：D2 编排与 Web 链路已通过真实双分支试跑；数据兼容性问题转 D3
> 更新日期：2026-08-06
> 当前回归结果：`189 passed, 1177 warnings in 10.59s`

## 1. 交付结论

D2 已按冻结的 `m2.batch.v1` 完成单机批量链路：

```text
固定 endpoint_id + Revision
-> 服务端重建完整 M1 候选
-> modified 项有界调度并复用现有 m2.diff.v1
-> 每项独立持久化并发布 result_ref
-> Web 轮询任务并按需读取工作簿明细
```

正式比较页不接受或提交候选清单。浏览器只提交两侧端点与具体 Revision，批量任务、候选状态和结果引用均由服务端产生。

## 2. 已实现范围

- 严格批量 Schema：创建、任务、单项、取消和重试请求；未知字段拒绝；
- API：创建、查询、结果读取、取消和重试，任务与结果支持 ETag；
- 四类候选：`modified/left_only/right_only/read_error`，相同哈希文件不进入清单；
- 状态分层：`partial/failed -> business_failed + result_ref`，编排失败无结果引用；
- SQLite WAL 元数据与独立 gzip `m2.diff.v1` 文件；
- 30 天保留、7 天墓碑、孤立结果清理和重启租约恢复；
- 全局并发 2、单任务并发 1、600 秒超时、单项失败隔离；
- 取消不强杀运行项，重试创建新的子任务并保留两级来源 ID；
- 正式 Web 创建批量任务、刷新恢复、进度展示、取消、默认重试和按需读取结果；
- 保留结果页“重新比对当前工作簿”的单项能力。

## 3. 主要实现文件

- `app/schemas/batch.py`
- `app/services/batch_store.py`
- `app/services/batch_diff_service.py`
- `app/api/batch.py`
- `app/services/snapshot_service.py`
- `app/static/compare.js`
- `app/static/compare_results.js`
- `app/static/compare_results_batch.js`
- `app/static/compare_results_batch.css`
- `tests/contract/test_batch_diff_api.py`

固定 AtlasConfig 网页验收入口由 `app/tools/batch_web_sample.py` 提供，不接真实 SVN，也不修改正式 Provider 边界。

## 4. 自动化验收

新增 10 项批量契约测试，分组覆盖：

- 严格请求、固定 Revision、服务端候选重建与幂等冲突；
- 四类候选、相同哈希排除和稳定指纹；
- 成功、业务失败、编排失败、超时和结果保存失败；
- 原始 `m2.diff.v1` 读取、`result_ref`、SHA-256 与 ETag；
- 取消不强杀、默认重试、不可重试项拒绝；
- 租约一次恢复、第二次耗尽、跨 Store 全局并发限制；
- 重启持久化、30 天到期清理和任务/结果墓碑。

全量命令：

```powershell
py -3 -m pytest -q
```

结果：

```text
189 passed, 1177 warnings in 10.59s
```

警告仍为 FastAPI/Starlette 既有弃用警告。

## 5. 浏览器验收

固定 AtlasConfig 批量链路实际结果：

- 任务终态：`completed`；
- 进度：`1 / 1`；
- 工作簿：`AtlasConfig.xlsm`；
- 明细：16 Sheet、273 修改行、375 修改字段；
- 刷新后恢复同一 `task_id`；
- 1440px 桌面和 390px 移动端无页面横向溢出；
- 浏览器控制台零错误。

本轮内置浏览器 Node 运行时受 Windows `CreateProcessWithLogonW 1385` 阻断，因此使用本机 Playwright + Edge 完成同等验收。截图保存在 `.cache/d2-batch-desktop.png` 和 `.cache/d2-batch-mobile.png`。

本地启动命令：

```powershell
py -3 -m app.tools.batch_web_sample --port 5573
```

入口：`http://127.0.0.1:5573/__local_verify/atlas-batch`

## 6. 边界

D2 没有修改 `core/`、`m2.diff.v1`、Excel/CSV 解析语义或 SVN 只读边界。分布式队列、跨节点 Worker、优先级、配额、暂停、长期历史报告、搜索、导出和管理后台继续归 M3。

D2 后续真实双分支试跑为 54/54 已处理、25 成功、29 业务失败、0 编排失败，证明批量编排与页面链路可用。完整证据见 `M2-05-STAGE-D2-REAL-DATA-TRIAL-REPORT.md`；五类解析/数据兼容性问题转入 `M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md`，不在 D2 调度层修正。
