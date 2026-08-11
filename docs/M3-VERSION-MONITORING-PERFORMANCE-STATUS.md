# M3 版本监控性能改造状态

> 分支：`codex/m3-report-performance`
> 基点：`afebaf3ef56a897125ab734d9a27307365685d70`
> 更新日期：2026-08-11
> 当前状态：Mock 影子门禁通过，尚未执行真实 SVN 测量，正式 Runner 仍使用旧引擎

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

## 自动化证据

- 新增增量与诊断测试：18 passed。
- M2/M3 聚焦回归：207 passed。
- 排除 5 个本机私有 `config/settings.json` 依赖文件后的完整回归：398 passed。
- Python 语法检查通过。

覆盖场景：CSV-only 多提交、最终回退、manifest Sheet 新增/删除、重新配对、
`tbxName` 改名、CSV 删除后恢复、工作簿删除重建、共享 CSV 多 owner、大小写
匹配冲突、局部 CSV 解析失败、unknown author、unresolved、无提交终点兜底、
缺失 changed paths、目录级变化和无关路径。

## 尚未完成

- 未访问真实 SVN，未测量 `r26475 -> r26514`。
- 未建立真实冷/暖缓存耗时、CPU、峰值工作集、命令数和读取字节对照。
- 未把增量引擎接入正式 Runner；旧引擎仍是唯一正式路径。
- 未根据真实计时决定是否优化 SVN CLI 探测、并发读取或 Excel Parser。

正式切换必须等待真实基线逐条一致，并继续保持
`m3.monitor-report.v1`、summary、稳定排序、错误/partial、publication、latest、
Windows 调度、固定分支、固定 Revision、左开右闭区间和 SVN 只读语义。
