# AGENTS.md

## 项目

本项目用于搭建一个进行Excel的DIff/Merge平台。协助游戏项目策划进行版本比对与差异判断

## 文档路由

- 维护左侧导航“版本对比”模块时，先读取 `docs/VERSION-COMPARISON-HANDBOOK.md`。
- 维护、扩展、验收或排查“版本监控”、Windows 计划任务、独立 Runner、增量回放或 M3 报告时，先读取 `docs/M3-VERSION-MONITORING-HANDBOOK.md`；仅涉及产品语义变化时再读取 `docs/M3-VERSION-MONITORING-PRD.md`。
- 维护“历史任务”页面、任务恢复、任务事件、运行日志或 SVN 缓存治理时，再读取 `docs/HISTORY-TASKS-HANDBOOK.md`。
- 数据契约以 `docs/contracts/`、`docs/adr/ADR-006-m1-head-freeze-table-excel.md` 和 `docs/adr/ADR-007-m2-table-tablecsv-pairing.md` 为准。
- `docs/archive/m2-history/` 是已完成阶段的历史材料。除历史审计、回归根因或契约迁移外，不默认读取或扫描。
- `docs/archive/m3-history/` 保存 M3 分阶段实施、验收和性能探索过程。除追溯历史决策、回归根因或性能基线外，不默认读取或用其覆盖当前手册、PRD、契约和代码。
- 不因契约名仍为 `m2.diff.v1`、`m2.batch.v1` 而恢复旧阶段工作流；模块后续统一称为“版本对比”。
