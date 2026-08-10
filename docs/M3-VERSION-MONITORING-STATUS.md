# M3 版本监控阶段状态

> 主控分支：`codex/m3-version-monitoring-report`
> 更新日期：2026-08-10
> 事实来源：PRD、实施计划、阶段提交和本文件

## 当前状态

| 阶段 | 状态 | 阶段分支 | 验收提交 | 结果 |
|---|---|---|---|---|
| Planning | 已完成 | `codex/m3-version-monitoring-report` | 本文件所在提交 | PRD 与实施计划已冻结 |
| Phase 0 | 待开始 | `codex/m3-p0-contracts` | - | - |
| Phase 1 | 未开始 | `codex/m3-p1-diff-engine` | - | - |
| Phase 2 | 未开始 | `codex/m3-p2-runner-store` | - | - |
| Phase 3 | 未开始 | `codex/m3-p3-report-lifecycle` | - | - |
| Phase 4 | 未开始 | `codex/m3-p4-windows-scheduler` | - | - |
| Phase 5 | 未开始 | `codex/m3-p5-monitor-ui` | - | - |
| Phase 6 | 未开始 | `codex/m3-p6-real-acceptance` | - | - |

## 主控规则

1. 每个 Phase 使用独立 Codex 任务、工作树和阶段分支。
2. 阶段任务只读取 `AGENTS.md`、M3 PRD、实施计划中的当前 Phase、已冻结契约和上一阶段交接。
3. 阶段任务必须提交代码、清理工作区并报告测试结果后才能申请验收。
4. 主控按实施计划门禁审查提交；不通过时返回原阶段任务修复。
5. 通过后将阶段提交合入主控分支，更新本文件，再从新的主控 HEAD 创建下一阶段任务。
6. 产品语义变化先更新 PRD，再允许阶段实现继续。

## 阶段交接格式

```text
阶段：Phase N
任务：<Codex task title/id>
分支：<branch>
提交：<commit SHA>
完成范围：<summary>
测试：<commands and results>
遗留问题：<none or details>
工作区：clean
```

## 验收记录

阶段通过后按时间顺序追加，至少记录阶段、提交 SHA、验证命令、结论和已知限制。
