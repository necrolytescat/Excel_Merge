# 版本对比性能验收

## 1. 适用范围

本文记录版本对比批次 1 的可重复性能入口、报告口径和当前基线。性能工具只使用已登记的离线 Replay 夹具，不访问 SVN、不创建正式任务、不更新黄金结果。

默认夹具：`var/m2-fixtures/d3c-be317423.m2fixture`

约束保持不变：

- `m2.diff.v1`、`m2.batch.v1` 和正式结果 gzip 格式不变；
- `source=left`、`target=right`，Excel 与 CSV 仍来自同侧同一冻结 Revision；
- 不执行 Merge、写回、宏或公式；
- 不修改 core 解析、配对、主键或 Diff 语义；
- 不跳过校验、不减少候选、不丢弃错误。

## 2. 固定命令

五轮离线 Replay：

```powershell
py -3 -m app.tools.version_comparison_performance_safe `
  --rounds 5 `
  --output <report-directory>\batch1-replay-5-rounds.json
```

临时批量链路验收：

```powershell
py -3 -m app.tools.version_comparison_batch_acceptance_safe `
  --output <report-directory>\batch1-local-batch-acceptance.json
```

专项回归：

```powershell
py -3 -m pytest `
  tests/contract/test_batch_diff_api.py `
  tests/unit/test_batch_store_connection_lifecycle.py `
  tests/unit/test_diff_performance.py `
  tests/unit/test_diff_performance_resolver.py `
  tests/unit/test_diff_performance_probe.py `
  tests/unit/test_version_comparison_performance_safe.py `
  tests/unit/test_version_comparison_performance_statistics.py `
  tests/unit/test_version_comparison_batch_acceptance_safe.py -q
```

安全入口对意外异常只输出固定错误码，避免把仓库 URL、Revision 或工作簿名写入报告。正式 `create_app` 不装配计时适配器，因此默认服务路径没有新增观测开销。

## 3. 口径

Replay 报告 schema 为 `m2.version-comparison-performance.v1`：

- `load`：夹具加载和严格验证耗时；
- `runs[].all_results_seconds`：一轮 55 项全部重算完成时间；
- `runs[].first_result_seconds`：该轮首项序列化完成时间；
- `runs[].cpu_seconds`、`peak_working_set_bytes`、`process_io`：进程 CPU、进程生命周期峰值工作集和本轮进程 I/O 增量；
- `runs[].item_seconds`：单工作簿 min/P50/P95/max，P95 使用 nearest-rank；
- `runs[].performance.phases`：清单解析、CSV 读取/解析、语义 Diff、序列化、物化及清理的累计计时；
- `runs[].result_set_sha256`：按固定结果顺序拼接每项 SHA-256 后得到的集合哈希。

冷态是新基准进程中的第一轮；热态是同一进程、同一夹具的后续轮次。离线 Replay 不经过 SVN，因此该冷/热口径只反映进程与操作系统文件缓存，不代表 SVN 磁盘缓存冷/热。

临时批量报告 schema 为 `m2.version-comparison-batch-acceptance.v1`。它使用真实 55 项 Replay 输入、正式 `BatchDiffService`、临时 `BatchStore` SQLite 和 gzip 结果文件。退出时必须满足 `temporary_state_removed=true`，且不写正式数据库。

阶段累计时间可能嵌套，例如 `diff.csv_parse` 包含于 `diff.csv_read_parse`，不能直接相加为端到端耗时。

## 4. 批次 1 基线

环境：Windows、Python 3.14，同机存在项目服务进程；结果用于当前机器同负载下的批次间对比。

代表样本包含 55 个工作簿、728 个输入文件，夹具大小 46,218,610 bytes。2026-08-12 五轮结果：

| 指标 | 结果 |
|---|---:|
| Replay 一致性 | 每轮 55 matched / 0 mismatched |
| 结果集合 SHA-256 | `d9b9fd7f3c02ef6fc47081d03c7d670ee4bddfbb55ad2a45e10a95bf7e4fda0f` |
| 全部完成时间 | P50 27.49s，范围 27.29s-28.11s |
| 首结果时间 | 0.036s-0.052s |
| 单工作簿耗时 | P50 0.081s，P95 1.708s |
| 进程 CPU | P50 8.17s |
| 峰值工作集 | 最高 747.1 MB |
| 每轮进程 I/O | 平均读取 83.1 MB，写入 83.2 MB |
| main 清单解析 | P50 22.22s |
| CSV 解析 | P50 4.01s |
| 语义 Diff | P50 0.21s |
| 序列化 | P50 0.07s |
| 物化 / 清理 | P50 0.38s / 0.16s |

本地批量链路结果：任务 `completed`，55 matched / 0 mismatched，首结果 0.17s，全部完成 33.41s，结果集合 SHA 与 Replay 相同，临时状态已回收。报告包含 gzip/fsync、SQLite 创建、准备、claim/complete、租约恢复和结果加载阶段。本次短间隔验收中 `recover_expired_leases` 调用 284 次、累计 11.42s；该数值与其他存储阶段嵌套，不能从端到端耗时直接相减，但说明调度轮询开销值得后续单独评估。

基准数据具有环境噪声。后续批次必须在同机器、同服务负载、同夹具和同进程策略下至少运行五轮，以中位数为主，P95 和最大值用于识别尾延迟。

## 5. 当前缺口

离线夹具无法测量正式 SVN 的 `list_tree`、`list_children`、工作簿读取、CSV `cat`、SVN 磁盘缓存冷/热和网络等待。本批次已提供 opt-in Provider/Resolver 计时适配器，但不得为补齐数据自行访问 SVN 或创建正式任务。正式链路的首结果、取消、超时、租约恢复、部分失败和服务重启仍沿用现有自动化测试；真实正式任务性能需另行授权。

批次 1 没有改变数据库 schema、SQL 状态机、磁盘缓存格式、公开 API 或进程模型。唯一生命周期修复是确保每次 SQLite 上下文退出时显式关闭连接，以保证 Windows 能回收临时状态目录。

## 6. 后续批次门禁

进入批次 2 前必须满足：

- 全量 Replay 每轮 55/55/0，结果集合 SHA 与本页基线一致；
- 专项和全量自动化测试通过；
- 临时批量任务完成且 `temporary_state_removed=true`；
- 报告不包含 SVN URL、Revision 或工作簿名；
- `git diff --check` 通过；
- 任何收益结论都区分 Replay 本地计算和正式 SVN I/O，不用离线结果替代正式链路证据。

## 7. 2026-08-17 首次运行提速增量验收

### 已实现

- OOXML Manifest 首选，openpyxl 回退，并保留双解析一致性门禁。
- 缓存 v2：内容寻址 blob、完整树、`PRESENT/MISSING/UNAVAILABLE`、原子 ready、single-flight、任务租约与 pinned LRU。
- 固定 Revision 的 Excel/CSV Frozen Dataset；同分支按 last-changed Revision 增量复用，跨分支由完整 SVN 差异证据控制。
- 大批量 export、小批量 12 路 cat、export 漏项仅回退缺失文件。
- ready 后工作簿阶段完全本地读取；Manifest 复用；SVN 内容调用门禁为 0。
- M2/M4 共享 SQLite 公平四路调度、lease token、防迟到提交和一次恢复。
- 八阶段内部耗时事件与三个独立灰度开关。

### 五轮 Replay

报告：`.cache/codex-p5-manifest-reuse.json`。

- 5/5 轮均为 55 matched / 0 mismatched。
- 集合 SHA-256 均为 `d9b9fd7f3c02ef6fc47081d03c7d670ee4bddfbb55ad2a45e10a95bf7e4fda0f`。
- `all_rounds_passed=true`，`unique_result_set_sha256=1`。
- legacy equivalent P50：7.890093s。
- Manifest reused P50：6.799458s。
- P50 加速：1.167x；节省 1.124231s。
- 峰值工作集：722,731,008 bytes。
- 报告确认未写 SVN、批量数据库或黄金夹具。

全仓自动化测试：`721 passed`。

### 真实环境门禁

原完整首次对比基线为 218.569s，其中快照 41.945s、55 个工作簿 176.422s；目标分别为首次完整链路不超过 109s、相同 Revision 重跑不超过 43.7s。

未经用户明确授权，本轮没有访问真实 SVN，也没有启动正式版本对比任务，因此两个真实环境目标尚不能宣称达成。正式发布仍按代码保持关闭 -> Replay/Mock -> 单次授权真实任务 -> 同分支增量 -> 跨分支复用 -> 四路并发 -> 默认开启推进。
