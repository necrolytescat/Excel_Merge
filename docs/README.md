# 项目文档导航

> 更新日期：2026-08-09

## 当前维护入口

维护左侧导航“版本对比”模块时，按以下顺序读取：

1. `VERSION-COMPARISON-HANDBOOK.md`：模块架构、语义、数据、修改入口和验证方法；
2. `contracts/m2.diff.v1.example.json`：单工作簿结果示例；
3. `contracts/m2.batch.v1.md`：批量任务契约；
4. `contracts/m2.batch.v1.example.json`：批量任务示例；
5. `contracts/m2.batch.v1.acceptance.md`：批量验收要求；
6. `adr/ADR-006-m1-head-freeze-table-excel.md`：端点 HEAD 冻结与 Table Excel；
7. `adr/ADR-007-m2-table-tablecsv-pairing.md`：同侧同 Revision 的 Table/TableCsv 配对；
8. `ENGINEERING-BASELINE.md`：当前不可突破的工程边界；
9. `ROADMAP.md`：已交付能力与后续范围。

若文档冲突，以自动化测试、当前实现、有效契约和 ADR 为准。

## 当前有效文档

| 文档 | 用途 |
|---|---|
| `VERSION-COMPARISON-HANDBOOK.md` | 版本对比模块长期工作手册和接手入口 |
| `ROADMAP.md` | 当前产品能力与后续方向 |
| `ENGINEERING-BASELINE.md` | 只读、方向、Revision、契约和测试基线 |
| `SVN-ENDPOINT-MODEL.md` | 端点注册和物理路径模型 |
| `M1-HANDOFF.md` | 已交付快照与文件级候选基线 |
| `contracts/` | 当前数据契约、示例和验收规范 |
| `adr/` | 已接受架构决策 |
| `acceptance-cases.md` | M0/M1 历史验收用例 |

契约文件仍以 `m2.*` 命名，这是稳定版本 ID，不表示模块仍处于 M2 阶段。

## 历史归档

`archive/m2-history/` 保存 M2 阶段的计划、评审、试跑、兼容性、前端交接、收尾前路线图和证据。默认不读取；仅在以下情况按归档索引定向查阅：

- 追溯某项规则的形成过程；
- 调查历史回归或数据兼容性；
- 迁移/升级现有契约；
- 审计最终任务与夹具来源。

M0/M1 的其他历史资料继续保留在现有路径。早期 PRD、调研报告和 `docs/verify/` 实验不能覆盖当前契约。

## 数据与生成物

| 路径 | 规则 |
|---|---|
| `tests/excel/left`、`tests/excel/right` | 固定本地 Excel/CSV 回归样例，只读使用 |
| `var/m2-fixtures/d3c-be317423.m2fixture` | 当前冻结 Replay 夹具，Git 仅跟踪这一份 |
| `var/m2-batch/` | 本地批量运行状态，不进入 Git |
| `.cache/` | 可再生输出，不是契约源 |
