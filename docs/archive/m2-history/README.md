# M2 历史归档索引

> 归档日期：2026-08-09
> 状态：阶段已完成；产品能力继续作为“版本对比”模块维护

## 使用规则

本目录用于历史沉淀，不是日常开发必读材料。维护版本对比时先读 `../../VERSION-COMPARISON-HANDBOOK.md`，只有需要追溯决策、调查历史回归或迁移契约时，才按本索引定向读取。

历史文档保留当时的路径、测试数、任务状态、夹具路径和待办，可能已经过期。当前事实以源码、测试、`../../contracts/`、ADR 和工作手册为准。

## 阶段结论

M2 已完成以下交付：

- Excel `main` + TableCsv 的工作簿/Sheet/行/字段语义 Diff；
- 冻结 Revision 的 SVN 只读数据集物化；
- `m2.diff.v1` 与 `m2.batch.v1`；
- 单机批量运行时、持久化、恢复、取消/重试和失败隔离；
- 正式差异结果页及工作簿、Sheet、行字段审阅体验；
- 离线 Replay 和最终完整夹具。

最终正式任务为 `be317423-3863-4cfe-aa6a-fc38ad50919f`，两侧 r26476，55 succeeded、0 failed。当前夹具为 `var/m2-fixtures/d3c-be317423.m2fixture`，728 输入、0 缺失、55/55 matched、0 mismatched，SHA-256 为 `092847df4c3b97f1026fe717d789a9f676e3352f1e27b904805df06682dfb0fc`。

## 文档分类

### 总体与基础

| 文件 | 历史用途 |
|---|---|
| `M2-HANDOFF.md` | 阶段总链路和实施顺序 |
| `M2-BACKEND-STATUS-HANDOFF.md` | 单工作簿后端接入状态 |
| `M2-00-WEB-ADAPTATION.md` | 初始 Web 工作台适配 |
| `M2-01-EXCEL-SAMPLE-VALIDATION.md` | AtlasConfig 样例规则与真值 |
| `M2-02-TO-M2-04-STABLE-DIFF-JSON-PLAN.md` | 稳定 JSON、解析和语义 Diff 计划 |

### Web 接入与批量运行时

| 文件 | 历史用途 |
|---|---|
| `M2-05-WEB-DIFF-INTEGRATION-CONTRACT.md` | 单工作簿 API、正式结果页和 SVN 适配契约 |
| `M2-05-STAGE-D-BATCH-DIFF-HANDOFF.md` | 批量契约设计启动交接 |
| `M2-05-STAGE-D1-REVIEW-HANDOFF.md` | 批量契约评审结果 |
| `M2-05-STAGE-D2-REVIEW-HANDOFF.md` | 批量运行时实现与验收 |
| `M2-05-STAGE-D2-REAL-DATA-TRIAL-REPORT.md` | 首次真实批量试跑与失败分类 |
| `evidence/M2-05-D2-REAL-TRIAL-6131d91a.json` | D2 真实试跑机器证据 |

### 数据兼容性

| 文件 | 历史用途 |
|---|---|
| `M2-05-STAGE-D3-A-COMPATIBILITY-REVIEW.md` | 真实数据问题分类与规则评审 |
| `M2-05-STAGE-D3-B-COMPATIBILITY-IMPLEMENTATION.md` | 文件名兼容和主键兜底实现记录 |
| `M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md` | D3 完成状态和旧夹具审计 |
| `M2-OFFLINE-FIXTURE-RUNBOOK.md` | 旧 D3-C 夹具与 Replay 操作记录 |

### 结果页改造

| 文件 | 历史用途 |
|---|---|
| `M2-08-DIFF-RESULT-FRONTEND-HANDOFF.md` | 结果页逐项改造全过程与锁定边界 |
| `M2-08-ROW-FIELD-SIDE-BY-SIDE-PLAN.md` | 左右对照行字段视图计划 |
| `M2-08-ROW-FIELD-DIFF-HANDOFF.md` | 行字段模块交接与最终变更 |
| `M2-08-ROW-FIELD-DIFF-ARCHIVE.md` | 已验收行字段结果页归档记录 |

### 收尾时快照

| 文件 | 历史用途 |
|---|---|
| `DOCS-INDEX-AT-M2-CLOSEOUT.md` | 收尾前文档导航 |
| `ROADMAP-AT-M2-CLOSEOUT.md` | 收尾前产品路线图与待办 |
| `ENGINEERING-BASELINE-AT-M2-CLOSEOUT.md` | 收尾前 M1/M2 工程基线 |

## 仍在当前目录外有效的契约

以下文件没有归档，仍是版本对比模块的有效依据：

- `../../contracts/m2.diff.v1.example.json`；
- `../../contracts/m2.batch.v1.md`；
- `../../contracts/m2.batch.v1.example.json`；
- `../../contracts/m2.batch.v1.acceptance.md`；
- `../../adr/ADR-006-m1-head-freeze-table-excel.md`；
- `../../adr/ADR-007-m2-table-tablecsv-pairing.md`。

契约 ID 中的 `m2` 保持不变，避免破坏现有 API、夹具和消费方。
