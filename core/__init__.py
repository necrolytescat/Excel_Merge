"""Phase 1 核心包：CSV 语义 diff + 按提交人归因 + HTML 报告。

设计原则（见 docs/PRD-Phase1-FIX分支全量对比报告.md）：
- diff 源为 SVN 上的 CSV，cells 按「代码名」键控（非列字母），消除插列假差异。
- 全程 stdlib，无第三方依赖；SVN 层后续接入，本地先用 revisions 列表驱动。
"""
