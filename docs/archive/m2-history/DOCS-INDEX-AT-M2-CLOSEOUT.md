# 项目文档目录与有效性说明

> 更新日期：2026-08-09
> 目的：按“当前契约、阶段交接、并行工作、历史参考”归类现有文档。
> 说明：本次只建立索引，不移动旧文件，避免破坏已有链接。

## 1. 建议阅读顺序

处理当前 M2 工作时，按以下顺序读取：

1. `ROADMAP.md`：产品阶段和下一里程碑；
2. `ENGINEERING-BASELINE.md`：已冻结的工程边界；
3. `archive/M2-08-ROW-FIELD-DIFF-ARCHIVE.md`：已验收的 M2-08 前端归档基线；
4. `M2-HANDOFF.md`：M2 总体流程；
5. `M2-BACKEND-STATUS-HANDOFF.md`：单工作簿与 SVN 接入完成状态；
6. `M2-05-WEB-DIFF-INTEGRATION-CONTRACT.md`：已完成的阶段 A/B/C 契约；
7. `M2-05-STAGE-D2-REVIEW-HANDOFF.md`：D2 实施与编排验收交接；
8. `M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md`：已完成的 D3 兼容性加固；
9. `M2-OFFLINE-FIXTURE-RUNBOOK.md`：Replay 夹具生成和验证规则；
10. `contracts/m2.batch.v1.md`：D1 已冻结、D2 已实现的批量任务契约；
11. `contracts/m2.diff.v1.example.json`：机器可读结果示例。

若文档冲突，以较新的 ADR、当前阶段交接和已执行的契约测试为准；早期 PRD 和调研文档不能覆盖当前契约。

## 2. 当前有效契约

| 文档 | 分类 | 当前用途 |
|---|---|---|
| `ROADMAP.md` | 产品主线 | 记录 M0/M1 归档、M2 阶段进度和 M2-07/M3 边界 |
| `ENGINEERING-BASELINE.md` | 工程基线 | 冻结 M1，只读安全和 M2 稳定 JSON 规则 |
| `M1-HANDOFF.md` | 已归档基线 | M1 端点、HEAD 冻结、`Table` Excel 候选契约 |
| `M2-HANDOFF.md` | M2 总交接 | Excel 定位、CSV 比值和 M2-00 至 M2-07 顺序 |
| `M2-BACKEND-STATUS-HANDOFF.md` | 后端完成状态 | 单工作簿链路、SVN 数据适配基线和批量边界 |
| `M2-05-WEB-DIFF-INTEGRATION-CONTRACT.md` | 已完成实施契约 | 单工作簿 API、正式结果页与冻结 Revision SVN 适配 |
| `M2-05-STAGE-D-BATCH-DIFF-HANDOFF.md` | D1 启动交接 | 批量设计前置约束，D1 已按此执行 |
| `M2-05-STAGE-D1-REVIEW-HANDOFF.md` | 历史评审交接 | D1 已评审通过并解除 D2 门禁 |
| `M2-05-STAGE-D2-REVIEW-HANDOFF.md` | 已完成阶段交接 | D2 已完成，记录实现、测试和浏览器验收 |
| `M2-05-STAGE-D2-REAL-DATA-TRIAL-REPORT.md` | 真实试跑报告 | 冻结 54 项结果、五类失败统计和证据位置 |
| `M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md` | 已完成阶段交接 | D3 真实数据分类、兼容规则和 Replay 验收结果 |
| `M2-OFFLINE-FIXTURE-RUNBOOK.md` | Replay 操作规范 | 离线夹具生成、校验、黄金更新和禁止事项 |
| `M2-08-DIFF-RESULT-FRONTEND-HANDOFF.md` | 已完成阶段交接 | M2-08 前端逐项改造过程、锁定边界和归档结论 |
| `M2-08-ROW-FIELD-DIFF-HANDOFF.md` | 已归档模块交接 | 行与字段差异模块的历史启动上下文和最终状态 |
| `archive/M2-08-ROW-FIELD-DIFF-ARCHIVE.md` | 已验收归档 | M2-08 行与字段模块交付能力、验证证据和剩余 M2 边界 |
| `M2-01-EXCEL-SAMPLE-VALIDATION.md` | 已完成验证 | AtlasConfig 固定样例、规则和真值 |
| `M2-02-TO-M2-04-STABLE-DIFF-JSON-PLAN.md` | 已完成实施 | JSON 契约、解析器、Diff、测试和输出结果 |
| `SVN-ENDPOINT-MODEL.md` | 端点契约 | M1 端点模型及 M2 同端点同 Revision 配对 |
| `adr/ADR-006-m1-head-freeze-table-excel.md` | 已接受 ADR | M1 确认时冻结 HEAD |
| `adr/ADR-007-m2-table-tablecsv-pairing.md` | 已接受 ADR | `Table`/`TableCsv` 自动配对 |
| `contracts/m2.diff.v1.example.json` | 数据契约示例 | Web、API 和报告消费方的字段参考 |
| `contracts/m2.batch.v1.md` | 批量数据契约 | 任务/单项状态机、API、result_ref、存储、恢复和里程碑归属 |
| `contracts/m2.batch.v1.example.json` | 批量契约示例 | 机器可读的完整终态任务示例 |
| `contracts/m2.batch.v1.acceptance.md` | 批量验收契约 | D2 必须自动化的契约、恢复、并发和只读用例 |

## 3. Web 改造与接入状态

| 文档 | 状态 | 说明 |
|---|---|---|
| `M2-00-WEB-ADAPTATION.md` | 已完成交接 | 页面布局、状态、交互和全量比对工作台 |
| `M2-05-WEB-DIFF-INTEGRATION-CONTRACT.md` | 阶段 A/B/C 已完成 | 单工作簿真实 Diff API、正式结果页与 SVN 数据物化已接入 |
| `M2-05-STAGE-D1-REVIEW-HANDOFF.md` | 阶段 D1 已评审通过 | 已形成批量契约、示例和验收用例 |
| `M2-05-STAGE-D2-REVIEW-HANDOFF.md` | 阶段 D2 已验收 | 单机批量运行时、正式页面和 54 工作簿编排链路已完成 |
| `M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md` | 阶段 D3 已完成 | 真实数据分类、文件名兼容和第一列主键受限兜底已验收 |
| `M2-08-DIFF-RESULT-FRONTEND-HANDOFF.md` | 已验收归档 | 全部用户确认的 M2-08 前端改造已完成并锁定 |
| `M2-08-ROW-FIELD-DIFF-HANDOFF.md` | 已验收归档 | 左右对照、字段表头、字符标红和字段视图切换已完成 |

M2-05 接入可以修改契约规定的 Web、API 和测试文件，但不能重新定义以下规则：

- `source=left`、`target=right`；
- `sheetName -> Id/id -> 字段名` 精确匹配；
- Excel 与 CSV 来自同一端点、同一冻结 Revision；
- 完整工作簿明细使用 `m2.diff.v1`；
- 全量比对是对 M1 候选逐工作簿执行，不是扫描全部 CSV。

## 4. 阶段归档、运维与验收资料

| 文档 | 用途 |
|---|---|
| `M0-BASIC-SVN-CONFIG.md` | M0.1 SVN 基础配置归档 |
| `M0-RUNBOOK.md` | 本地 Web 与 SVN 基座运行说明 |
| `MVP-PRD.md` | M1 已归档产品需求 |
| `acceptance-cases.md` | M0/M1 验收用例和历史基线 |
| `archive/M0.1-ARCHIVE.md` | M0.1 归档记录 |
| `archive/M2-08-ROW-FIELD-DIFF-ARCHIVE.md` | M2-08 行与字段差异模块归档记录 |

这些文档用于维护已交付能力，不作为新增 M2 语义规则的来源。

## 5. 调研与历史参考

| 文档 | 适用范围 |
|---|---|
| `调研报告.md` | SmartDiff、第三方库和早期技术风险调研 |
| `PRD-Phase1-FIX分支全量对比报告.md` | 早期全量报告方案，仅供参考 |
| `SVN-KR-DEV-STRUCTURE.md` | KR/DEV 历史目录和数量实测 |
| `verify/v1_duplicate_id.py` | SmartDiff 重复 ID 风险验证 |
| `verify/v2_celltype_fidelity.py` | SmartDiff 类型保真验证 |
| `verify/v3_column_shift.py` | SmartDiff 插列误判验证 |

其中的日期输入、文件名归属推断、同 URL 双 Revision、行号兜底或旧 CSV 流程均不能覆盖 ADR-006、ADR-007、`m2.diff.v1` 和 `m2.batch.v1`。

## 6. 生成物与测试样例

| 路径 | 分类 | 规则 |
|---|---|---|
| `tests/excel/left` | 固定 source 样例 | 只读 |
| `tests/excel/right` | 固定 target 样例 | 只读 |
| `.cache/m2/AtlasConfig.diff.json` | 可再生结果 | 不是手工维护的契约源 |

固定结果 SHA-256：

```text
430e5ae560fa6c83d6580e40f9e635294149cbdd9cacfb7020df9a2acb59a1e7
```

## 7. 当前修改规则

- 原 Web 与后端并行开发已经结束；
- M2-05 阶段 A/B/C 已统一完成 Web、API、请求 schema、SVN 数据适配和测试；
- M2-05 不修改 `core` Diff 规则、Excel/CSV 解析规则或 `m2.diff.v1` 响应语义；
- M2-05 执行期间，其他对话不要并行修改同一批 Web/API 文件；
- 阶段 C 完成后停止，批量任务必须在独立的阶段 D 契约下实施；
- 阶段 D1 已评审通过，D2 已按冻结契约完成并通过真实编排验收；
- D3 已完成真实数据兼容性分类和引擎加固；M2-08 已按用户逐项确认完成并归档，未修改批量调度、方向语义或 SVN 只读边界。
