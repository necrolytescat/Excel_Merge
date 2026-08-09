# M2 开发交接说明：Table Excel 候选与 TableCsv 语义 Diff

> 状态：M2-07 单机批量链路已完成，D2 等待评审  
> 前置里程碑：M1 已归档（2026-08-05）

## 1. M1 已交付基线

- 用户输入两个 SVN 分支目录名，从系统配置匹配的主干/FIX 候选或已登记端点中确认端点；
- 点击确认时分别冻结当前 HEAD，后续读取统一使用具体 Revision；
- 读取范围固定为逻辑目录 `Table` 下的 `.xlsx`、`.xlsm`、`.xls`；
- `Table` 物理路径在确认后回写端点注册表；
- 两份快照包含路径、作者、时间、文件 Revision、大小、内容引用和 SHA-256；
- 03 区域按逻辑相对路径和内容哈希筛选文件级差异候选；
- 真实 KR FIX 验证：两端各 197 个 Excel，候选 53 个，物理路径为 `Source/table`；
- SVN 全程只读；全量测试 114 passed。

## 2. M2 目标与主链路

M2 将 Excel 与 CSV 分工处理：Excel 用于 M1 文件级候选筛选、读取 `main` 导出清单和恢复展示结构；可靠导出的 CSV 用于业务值 Diff。

```text
冻结左右端点 Revision
→ 比较 Table 下 Excel，筛选文件级候选
→ 读取候选 Excel 的 main Sheet
→ sheetName 定义逻辑 Sheet，tbxName 定位 {tbxName}.csv
→ 在同端点、同冻结 Revision 的 TableCsv 下读取左右 CSV
→ 按字段名和 Id/id 计算行与字段 Diff
→ 按工作簿 → sheetName → 行 → 字段展示
```

M2-01 先用本地成对样例纵向验证，不接 SVN：

```text
left  = tests/excel/left/AtlasConfig.xlsm  + 同目录 16 个 CSV
right = tests/excel/right/AtlasConfig.xlsm + 同目录 16 个 CSV
```

方向固定为 `source=left`、`target=right`，不根据文件时间推断 `old/new`。

## 3. M2-00 Web 前端适配

先调整当前 Web 的信息架构和布局，为单文件语义 Diff 提供稳定入口。M2-00 只交付工作台骨架和页面状态，不伪造尚未确认的 Diff 数据契约。详细规格见 `docs/M2-00-WEB-ADAPTATION.md`。

页面结构固定为：

1. 版本输入与快照：保留 M1 两端点输入和冻结结果；
2. 文件级候选：沿用 M1 的差异候选清单；
3. 单文件 Diff 工作台：可从候选清单进入，也可在开发模式选择本地样本对；
4. Sheet 导航：为 Sheet 状态和差异数量预留位置；
5. 行与单元格差异详情：为后续语义引擎结果预留主视区和详情区。

M2-00 不计算工作簿、Sheet、行或单元格差异，不改变 M1 快照接口。

## 4. 已确认语义规则

- `main` 是导出清单，不参与业务内容 Diff；`配置公式2`、公式、格式、宏和隐藏状态也不参与；
- `sheetName` 是逻辑 Sheet 匹配与展示名，`tbxName` 定位 `TableCsv/{tbxName}.csv`；
- `tbxName` 改名但 `sheetName` 相同时，仍视为同一逻辑 Sheet；
- CSV 第 2 行是字段名、第 3 行是字段类型、第 4 行是字段范围、第 8 行起是业务数据；
- 列按字段名匹配，行主键按 `Id`、`id` 顺序识别，不按行号对齐；
- CSV 缺失、重复主键、结构非法或解析失败必须报告失败，不能当成无差异；
- CSV 来源可靠，Excel 不再承担业务单元格值比较。

## 5. 正式实施顺序

### M2-00 Web 前端适配

完成版本输入、候选清单和单文件 Diff 工作台的信息架构及响应式布局，定义 loading、empty、error、ready 页面状态。

### M2-01 单文件样本与纵向验证

以 `tests/excel/left` 和 `tests/excel/right` 中的 AtlasConfig Excel+CSV 数据集为固定样本，验证 `main` 映射、CSV 结构、主键和 Diff 结果。原始样例只读使用；首轮只验证这一对数据集，不遍历 SVN 候选。详细执行说明见 `docs/M2-01-EXCEL-SAMPLE-VALIDATION.md`。

### M2-02 Diff 契约

确定工作簿、Sheet、行、字段和失败项模型，冻结 `m2.diff.v1` 稳定 JSON 契约，并准备真实样本和边界样本。M2-02 至 M2-04 的纵向计划见 `docs/M2-02-TO-M2-04-STABLE-DIFF-JSON-PLAN.md`。

### M2-03 清单与 CSV 解析层

使用成熟 Excel 库只读解析候选工作簿的最小 `main` 清单；失败时验证 OOXML 兜底。使用标准 CSV 解析器读取字段定义和数据区。解析器不依赖 SVN Provider。

### M2-04 语义 Diff 引擎

按 `sheetName`、`Id/id` 和字段名计算 CSV 净差异，避免按文件名、行号或列位置直接比较造成差异雪崩。

### M2-05 单文件工作台接入

已用 M2-01 的样本接通正式工作台，展示 Sheet、行、字段差异，并覆盖 loading、empty、error 和真实结果映射。

### M2-06 M1 快照内容访问与 SVN 集成

该能力已在 M2-05 阶段 C 完成：复用 M1 已冻结的端点与 Revision，从 `Table` 候选工作簿读取 `main`，并从同一端点同一 Revision 的 `TableCsv` 读取对应 CSV。不重新解析 HEAD，不允许 Excel 与 CSV 分别选择端点。

### M2-07 报告输入

已完成 `m2.batch.v1` 单机批量创建、查询、结果读取、取消/重试、持久化、重启恢复、有界并发和失败隔离。每个 `modified` 工作簿继续独立生成原 `m2.diff.v1`，批量层只保存摘要和 `result_ref`；正式结果页负责轮询任务并按需读取明细。

M2 仍不执行 Merge 或 SVN 写入；分布式队列和长期报告管理继续归 M3。

## 6. 固定约束

- 不修改 M1 的 HEAD 冻结、端点注册和物理路径绑定契约；
- M1 仍不扫描 CSV；M2 批量层只读取服务端重建候选中各工作簿明确映射的 CSV；
- 不执行 `svn commit`、`merge`、`update`、`copy`；
- 不以行号、列位置或模糊相似度作为正式匹配依据；
- 解析失败必须进入失败清单，不能让整个任务静默返回空差异；
- 语义引擎必须可用本地 Excel+CSV 样本独立测试，不能强依赖 SVN；
- M2-00 只定义页面结构和状态，不以假数据固化正式 API 字段。

## 7. 旧文档适用性

`docs/PRD-Phase1-FIX分支全量对比报告.md` 中的 CSV、同 URL 双 Revision、日期输入和逐提交归因方案是早期调研参考，不覆盖当前 M1/M2 契约。当前有效约束以 `M1-HANDOFF.md`、本文件、`ROADMAP.md` 和相关 ADR 为准。

## 8. 当前完成状态

AtlasConfig 本地 Excel+CSV 数据集已经生成稳定 `m2.diff.v1` JSON，固定统计为 16 个 Sheet、56 个 source-only 行、39 个 target-only 行、273 个修改行和 375 个修改字段。完整输出位于 `.cache/m2/AtlasConfig.diff.json`，SHA-256 为 `430e5ae560fa6c83d6580e40f9e635294149cbdd9cacfb7020df9a2acb59a1e7`。

本地契约、解析器、Diff 引擎、冻结 Revision SVN 数据适配、单机批量运行时和正式批量结果页均已完成。全量测试为 `188 passed`；AtlasConfig 浏览器验收为 1/1 工作簿完成、16 Sheet、273 修改行和 375 修改字段。D2 实施记录见 `M2-05-STAGE-D2-REVIEW-HANDOFF.md`。
