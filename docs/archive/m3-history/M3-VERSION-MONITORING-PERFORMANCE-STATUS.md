# M3 版本监控性能改造状态

> 分支：`codex/m3-report-performance`
> 基点：`afebaf3ef56a897125ab734d9a27307365685d70`
> 更新日期：2026-08-11
> 归档状态：性能改造已完成；三轮真实门禁通过，正式 Runner 已切换增量引擎

## 已完成

1. 从最新本地 `main` 创建独立工作树和性能分支。
2. 建立默认关闭的阶段计时、计数和稳定语义指纹；观测数据不进入报告契约。
3. 实现 changed paths 分类、manifest 关系状态和 CSV 反向索引。
4. 实现一次完整起点状态后的逐提交局部回放：
   - CSV 变化更新全部 manifest owner Sheet；
   - Excel `main` 变化更新对应工作簿、Sheet 和重新配对关系；
   - 保留缺失/解析失败 CSV 的 expected owner，支持后续恢复；
   - 不相关路径不读取业务快照；
   - 目录级、路径缺失、未知动作和 owner 不可靠时全量兜底。
5. 实现旧引擎与增量引擎的稳定语义指纹比较。
6. 实现独立只读诊断入口 `app.tools.m3_performance_probe`：
   - 不导入或更新 MonitorStore；
   - 不渲染、发布或改写报告和 latest；
   - 不访问 Windows Scheduler；
   - 输出不包含 SVN URL、物理业务路径、缓存路径、文件内容或异常堆栈；
   - Revision、copy boundary 和结果计数不符时失败关闭。
7. 修复 Windows 64 位进程句柄签名，真实测量可采集峰值工作集。
8. 完成当前缓存影子、隔离空冷和隔离暖缓存三轮真实测量。
9. 生产 Runner 工厂显式选择增量引擎；旧引擎仅保留显式 `legacy` 诊断/测试路径。

## 自动化证据

- 新增性能、增量与 Runner 测试：22 passed。
- M3 history、Diff/归因、Runner/publication、调度、契约/API/页面：271 passed。
- manifest、TableCsv 和 Semantic Diff：37 passed。
- 排除 5 个本机私有 `config/settings.json` 依赖文件后的完整回归：404 passed。
- Python 语法检查和 `git diff --check` 通过。

覆盖场景：CSV-only 多提交、最终回退、manifest Sheet 新增/删除、重新配对、
`tbxName` 改名、CSV 删除后恢复、工作簿删除重建、共享 CSV 多 owner、大小写
匹配冲突、局部 CSV 解析失败、unknown author、unresolved、无提交终点兜底、
缺失 changed paths、目录级变化、无关路径，以及 Runner 原有 errors、partial、
publication、latest 和租约语义。

`tests/unit/test_offline_fixture.py` 仍在收集阶段硬依赖工作树内未提交的
`config/settings.json`，本轮未复制凭据或创建占位配置，因此未单独运行该文件。

## 真实影子基线

2026-08-11 使用隔离硬链接缓存执行一次当前缓存影子测量。SVN 操作严格只读，
单命令超时 30 秒、并发 1；未写 MonitorStore、报告、publication、latest 或
Windows Scheduler。测量前后共享缓存均为 11,013 个文件、1,222,579,653 bytes，
清单签名均为
`8ebb86f21eb175147cf29f06d0f21fab258788ce9eede80df2f2f185278f9301`。

语义门禁：

- 区间和分支身份：`r26475 -> r26514`，copy boundary `r26215`；
- SVN 历史：3 个提交、16 条 changed paths；
- 结果：197 个工作簿、197 个可靠工作簿、116 条最终净变化；
- 错误：0 errors、0 unknown author、0 unresolved；
- 旧/新语义指纹均为
  `6a5af718a2592524dac2212d64efc245ff38397740b62cfb363e0b55ff482749`；
- 候选范围：4 个工作簿、4 个 Sheet；0 次全量兜底。

性能结果：

- 诊断总 wall time 180.79 秒，CPU 21.48 秒；
- 旧引擎 147.13 秒，增量影子引擎 28.24 秒，约 5.21 倍加速；
- `svn info/list/cat` 分别为 2/8/6,785 次；`svn cat` 读取 510,759,442 bytes；
- 隔离缓存 delta：disk hit 3,869、memory hit 2,916、miss 0、write 0；
- 增量路径完成 201 次 manifest 解析和 778 次 CSV 解析。

## 隔离冷暖基线

同一固定输入使用新的空隔离缓存执行增量引擎，再原目录重复一次暖缓存。两轮均为
并发 1、SVN 只读，结果、指纹、候选范围和兜底次数与影子基线一致。

| 指标 | 空冷 | 暖缓存 |
| --- | ---: | ---: |
| wall time | 396.47 秒 | 32.59 秒 |
| CPU time | 13.06 秒 | 2.45 秒 |
| peak working set | 777,695,232 bytes | 776,531,968 bytes |
| cache hit / miss / write | 0 / 979 / 979 | 979 / 0 / 0 |
| `svn cat` wall time | 363.59 秒 | 0.36 秒 |
| 完整起点状态 | 383.72 秒 | 25.54 秒 |
| manifest 解析 | 17.33 秒 | 17.54 秒 |
| CSV 解析 | 3.45 秒 | 2.99 秒 |

隔离缓存包含 979 个文件、73,718,848 bytes。测后共享缓存出现了并发后台 Monitor
写入；只读审计确认其进程时间覆盖共享写入窗口，且共享近期文件与本轮隔离文件名
交集为 0。本轮诊断自身的 979 次写入全部落在隔离目录，未写正式存储或报告。

## 正式路径与优化决策

`P1MonitorRunEngineFactory` 现在显式构造 `incremental` 模式。Runner 仍先验证固定
分支身份并解析固定 start/end Revision，增量结果继续进入原有唯一报告渲染、错误
判定、history publication、latest 激活、租约和 MonitorStore 完成流程。

当前缓存影子证明正式计算约 5.21 倍加速。空冷时 `svn cat` 占总时间约 91.7%，
因此若继续优化首次运行，应优先验证有限并发读取或减少重复 SVN CLI 探测；暖缓存
时 manifest 解析约占 53.8%，是稳定运行的次级热点。当前没有更严格 SLA，且两类
优化都会扩大并发或 Parser 语义风险，本分支暂不实施。

## 结项时保留的非阻断验证缺口

- 增量切换后未再单独执行正式 MonitorStore、报告目录或 Windows 计划任务验收；
  真实影子门禁、完整 Runner 回归与用户决定已接受该缺口，不影响性能改造完成结论。
- 若后续要求优化空冷时间或峰值内存，需要另行设计有限并发/CLI 探测或 Parser
  实验，并重新执行真实只读门禁。

后续变更必须继续保持 `m3.monitor-report.v1`、summary、稳定排序、错误/partial、
publication、latest、Windows 调度、固定分支、固定 Revision、左开右闭区间和 SVN
只读语义。
