# M3 版本监控性能改造交接

> 归档状态：历史交接方案；报告页面与性能改造均已完成
> 更新日期：2026-08-11
> 前置诊断：`docs/archive/m3-history/M3-VERSION-MONITORING-PERFORMANCE-DIAGNOSIS.md`
> 一致性基线：`r26475 -> r26514`，197 个工作簿，116 条最终净变化，0 个错误

## 1. 用途和启动条件

本文是后续性能优化对话的实施入口。报告页面改造合入 `main` 后，新任务必须从最新 `main` 创建新的工作树和 `codex/m3-report-performance`，不能从当前旧的 detached HEAD 继续实现后再合并页面代码。

本次只改变“怎样更快得到同一个结果”，不改变最终净值、作者归因、manifest、固定 Revision、左开右闭区间、报告契约、错误覆盖或调度语义。

## 2. 最终确认的方向

当前 M3 会加载起点和终点完整快照，归因时又加载起点，并为区间内每个相关提交加载完整快照。区间有 `C` 个相关提交时，共请求 `C+3` 份完整快照，每份都遍历全部工作簿、解析 Excel `main`、读取 TableCsv 并执行 Diff。

这不是业务要求，而是初版的正确性优先实现。正式改造方向是：

```text
固定时间区间
→ 换算起止 Revision
→ 先读取区间提交及 changed paths
→ 找出受影响的 Excel 和 TableCsv
→ 通过 manifest 关系扩展到工作簿和 Sheet
→ 只读取、解析和比较受影响内容
→ 逐提交形成字段事件
→ 从事件中给最终净变化找作者
→ 发布原契约报告
```

两个相关提交只影响两个工作簿时，不应为每个提交重新比较 197 个工作簿。

## 3. changed paths 优先，HASH 辅助

M2 的 Excel 文件 HASH 是读取文件字节后由程序计算，不是 `svn list` 免费提供的内容 HASH。不能为了筛候选，先重新下载全部 Excel 再算 HASH。

M3 本来就需要读取 `svn log -v`，其中包含每个提交的 changed paths，因此 changed paths 是第一候选来源。

HASH 只做辅助：文件字节已经存在于可靠缓存时，可以排除内容完全相同的候选；不得为了算 HASH 新增一次全量读取。

## 4. 直接复用 M2 的规则

本次不重新实现配对和 Diff 语义，直接复用：

- `core/workbook_manifest_parser.py` 的 `sheetName / tbxName / isExport`；
- `core/table_csv_parser.py` 的字段、主键、scope、类型和值规范化；
- `core/semantic_diff.py` 的 Sheet、主键和字段比较；
- 精确文件名及唯一 `casefold` 匹配；
- Sheet 新增/删除、字段结构、行增删和字段修改；
- 同端点、同固定 Revision 的 Excel/TableCsv 配对。

M2 已解决两个固定端点的单工作簿比较。本次新增的是跨多个提交的关系索引和局部状态更新。

## 5. 需要建立的状态

运行期至少维护：

```text
workbook → 当前 WorkbookManifest
csv_path → {(workbook, sheetName)}
(workbook, sheetName) → 当前 tbxName / csv_path
(workbook, sheetName) → 当前 ParsedTableCsv 或可靠错误状态
```

同一 CSV 若被多个 Sheet 引用，必须记录全部所有者，不能假设一对一。

如果上一份报告的截止点正好是下一份报告起点，可以复用已验证的边界状态。缓存键必须包含仓库 UUID、规范分支、仓库相对路径、固定 Revision、Table/TableCsv 路径、dataset layout 和 Parser 版本。

缓存只是可再生加速数据。缺失、损坏或版本不匹配时，只能从同一固定 Revision 重建，不能改读 HEAD。

没有可复用缓存时，第一份报告允许在起点 Revision 建立一次完整状态，用于得到 197 个工作簿 inventory、建立反向索引并保持现有错误覆盖。完整起点只建立一次，后续提交只处理 changed paths。

## 6. 目标计算流程

### 6.1 区间和提交

1. 保持 Run 冻结的 `(start_at, end_at]`；
2. 验证固定分支身份；
3. 起止时间各换算一次 Revision；
4. 复用这组 Revision 查询提交；
5. XML commit time 继续精确过滤左开右闭区间；
6. 路径继续受固定分支和 copy boundary 限制。

不得把时间区间偷换成 Revision 闭区间，也不得混入其他分支路径。

### 6.2 路径分类

每个提交按顺序分为：

- Table 下 Excel 的新增、修改、删除、复制或移动；
- TableCsv 下 CSV 的新增、修改、删除、复制或移动；
- Table/TableCsv 目录级变化；
- 固定分支内其他无关路径；
- 无法可靠分类的路径。

无关路径直接跳过。目录级变化和无法分类的路径进入兜底，不能凭文件名猜范围。

### 6.3 CSV 变化

```text
changed csv_path
→ 查反向索引
→ 找到全部受影响 workbook / sheetName
→ 读取当前提交 Revision 的 CSV
→ 与上一状态做局部 Semantic Diff
→ 更新状态并记录事件
```

CSV 删除使用上一状态形成删除事件。新增 CSV 只有被当前 manifest 引用时才进入业务快照。

### 6.4 Excel manifest 变化

```text
changed workbook
→ 读取并解析当前 Revision 的 main
→ 比较旧、新 manifest
→ 识别 Sheet 新增、删除、tbxName 改名和重新配对
→ 更新 csv_path 反向索引
→ 只读取新增或重新绑定所需 CSV
→ 对受影响 Sheet 形成局部事件
```

`sheetName` 仍是业务身份，`tbxName` 只定位 CSV。Excel 样式、公式、宏和业务 Sheet 单元格仍不参与业务值比较。

### 6.5 最终净值和作者

全部提交处理完后，当前状态就是终点状态。最终只比较受影响工作簿和 Sheet，未受影响项沿用起点可靠状态，报告工作簿总数仍来自完整 inventory。

- `100 -> 120 -> 100` 不报告；
- `100 -> 120 -> 110` 报告 `100 -> 110`；
- 后续文件提交未改变目标字段时，不覆盖字段作者；
- 行新增、删除和结构变化继续使用形成最终状态的事件。

事件账本按 Revision 升序，以 `workbook + sheetName + row_key + field_name/change_type` 连接最终变化。无法可靠连接时仍为 `unresolved + partial`，不能猜作者。

## 7. 强制兜底

以下情况无法证明局部范围时，退回工作簿级或完整固定 Revision 快照：

- changed paths 缺失、截断或解析异常；
- Table/TableCsv 目录级复制、移动或批量替换无法展开；
- manifest 关系不唯一或无法安全更新；
- CSV 大小写匹配冲突；
- 分支身份、copy boundary 或路径归属不确定；
- 增量终点与固定终点校验不一致；
- 其他会改变现有错误覆盖的情况。

兜底原因进入脱敏诊断观测。不能为了性能把不确定情况当作无变化。

## 8. 实施阶段

### 阶段 0：从最新 main 重新开始

页面改造合入后，创建性能分支，读取最新 AGENTS、M3 手册、状态、本文、前置诊断、报告契约和页面交接。先确认页面是否改变 schema、summary 或稳定排序，并跑最新 M2/M3 回归。

### 阶段 1：只增加观测

记录总耗时、CPU、峰值内存、提交数、changed path 数、SVN 各命令次数/耗时/字节、缓存命中、Excel/CSV 解析次数/耗时、兜底次数和结果指纹。

观测不得进入报告契约，不得记录 URL、凭据、物理路径、文件内容、stderr 或堆栈。

### 阶段 2：候选规划和影子索引

实现 changed paths 分类、起点索引和受影响工作簿/Sheet 计算。旧全量引擎继续生成正式结果；新逻辑先只输出候选计划，用测试证明没有漏项。

### 阶段 3：增量回放引擎

实现 CSV 局部更新、Excel manifest 局部更新、反向索引维护和局部事件 Diff。旧引擎继续作为对照，不能立即删除。

### 阶段 4：新旧逐条核对

同一固定输入同时运行两套算法，比较 Revision 区间、变化身份、类型、前后值、作者、Revision、unknown/unresolved、errors、partial、summary 和稳定结果指纹。

全部门禁通过后才切换正式路径。旧引擎只可作为明确开关控制的诊断兜底，不能形成第二套报告契约。

### 阶段 5：按数据处理次级成本

只有计时证明需要时，再处理重复日期查询、重复身份验证、SVN CLI 探测、候选文件有限并发、OOXML `main` 快速路径和事件账本索引。这些不是主优化。

## 9. 一致性和测试门禁

真实基线必须保持：

- start/end Revision = `26475 / 26514`；
- workbook/reliable workbook = `197 / 197`；
- changes/errors = `116 / 0`；
- unknown/unresolved = `0 / 0`；
- 每条变化的工作簿、Sheet、类型、主键、字段、前后值、作者和 Revision 一致。

必须覆盖：中间回退、CSV-only 修改、manifest 新增/删除/重新配对、`tbxName` 改名但 `sheetName` 不变、行新增后修改、删除后重建、同一 CSV 多所有者、文件和目录复制移动、大小写匹配冲突、局部解析失败、unknown/unresolved、无变化区间、缓存失效重建和安全全量兜底。

至少运行 SVN history、M3 Diff/Attribution、Runner/publication、manifest/TableCsv/semantic diff、M2 Replay、M3 报告契约和页面改造后的展示回归。共享 Parser 或 semantic diff 有变化时必须扩大 M2 回归。

## 10. 与页面改造的边界

性能分支默认不修改报告 HTML/CSS/JavaScript、`m3.monitor-report.v1`、summary、稳定排序、页面 API、Windows 调度和逻辑边界。报告阶段计时放在 Runner 外围。

如果最新页面确实改变报告契约，先更新一致性基线和测试，再开始性能实现，不能在性能分支顺带修改页面语义。

## 11. 真实 SVN 测量

任何真实测量前，必须先向用户说明固定分支、Revision、只读命令、缓存方式、数据库/报告写入、预计时间、并发数和停止条件，并等待确认。

冷缓存只能使用新的隔离缓存目录，不能清空共享缓存。诊断入口不得发布报告、更新 latest、修改真实 MonitorStore 或触发 Windows 计划任务。

## 12. 完成标准

1. `r26475 -> r26514 / 197 / 116 / 0 errors` 逐条一致；
2. M2、M3 和页面相关回归全部通过；
3. 正常 changed paths 场景不再逐提交重建 197 工作簿；
4. 首次无缓存最多建立一次完整起点状态，后续只处理变化文件或明确兜底；
5. 有真实冷/暖分段数据解释时间、CPU、内存、SVN 调用和字节变化；
6. 报告契约、作者归因、manifest、固定 Revision、区间和调度语义不变；
7. 真实 SVN 严格只读，未污染正式数据库、报告和 Scheduler；
8. 文档更新、工作区干净、阶段提交可审查。

## 13. 新对话接手顺序

1. 确认页面改造已合入最新 `main`；
2. 创建新工作树和 `codex/m3-report-performance`；
3. 读取 AGENTS、M3 手册/状态、本文、前置诊断和页面交接；
4. 只读核对最新实现和报告契约；
5. 先实现观测与影子候选计划，不直接替换正式结果；
6. 在 Mock/Replay 上完成新旧逐条一致；
7. 说明真实 SVN 测量范围并等待确认；
8. 完成真实基线测量后，才逐阶段启用增量正式路径。
