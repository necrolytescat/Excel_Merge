# M3 版本监控性能改造状态

> 分支：`codex/m3-report-performance`
> 基点：`afebaf3ef56a897125ab734d9a27307365685d70`
> 更新日期：2026-08-11
> 当前状态：当前缓存真实影子门禁通过，正式 Runner 仍使用旧引擎

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
7. 修复 Windows 64 位进程句柄签名，后续测量可采集峰值工作集。

## 自动化证据

- 新增增量与诊断测试：21 passed。
- M2/M3 聚焦回归：207 passed。
- 排除 5 个本机私有 `config/settings.json` 依赖文件后的完整回归：401 passed。
- Python 语法检查和 `git diff --check` 通过。

覆盖场景：CSV-only 多提交、最终回退、manifest Sheet 新增/删除、重新配对、
`tbxName` 改名、CSV 删除后恢复、工作簿删除重建、共享 CSV 多 owner、大小写
匹配冲突、局部 CSV 解析失败、unknown author、unresolved、无提交终点兜底、
缺失 changed paths、目录级变化和无关路径。

## 真实影子基线

2026-08-11 使用隔离硬链接缓存执行一次已确认的当前缓存影子测量。SVN 操作严格
只读，单命令超时 30 秒、并发 1；未写 MonitorStore、报告、publication、latest
或 Windows Scheduler。测量前后共享缓存均为 11,013 个文件、1,222,579,653 bytes，
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
- `svn info/list/cat` 分别为 2/8/6,785 次；log 计数为 date 4、range 1、
  copy-boundary 1；`svn cat` 共读取 510,759,442 bytes；
- 隔离缓存 delta：disk hit 3,869、memory hit 2,916、miss 0、write 0；
- 增量路径完成 201 次 manifest 解析和 778 次 CSV 解析；峰值工作集本轮未取得。

结论：真实语义一致性和当前缓存性能门禁通过。当前主要收益来自 changed paths 将
逐提交事件回放限制到 4 个工作簿/Sheet；起点状态仍需完整加载。仅凭这一轮不能判断
冷缓存 SVN CLI、并发读取或 Excel Parser 的优先级，也不足以切换正式路径。

## 尚未完成

- 未建立独立空冷缓存与暖缓存重复测量；本轮历史结果缺少峰值工作集，采集器已修复，
  待下一轮测量验证。
- 未把增量引擎接入正式 Runner；旧引擎仍是唯一正式路径。
- 未根据真实计时决定是否优化 SVN CLI 探测、并发读取或 Excel Parser。

正式切换必须等待真实基线逐条一致，并继续保持
`m3.monitor-report.v1`、summary、稳定排序、错误/partial、publication、latest、
Windows 调度、固定分支、固定 Revision、左开右闭区间和 SVN 只读语义。
