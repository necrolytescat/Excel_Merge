# M3 版本监控阶段状态

> 主控分支：`codex/m3-version-monitoring-report`
> 更新日期：2026-08-10
> 事实来源：PRD、实施计划、阶段提交和本文件

## 当前状态

| 阶段 | 状态 | 阶段分支 | 验收提交 | 结果 |
|---|---|---|---|---|
| Planning | 已完成 | `codex/m3-version-monitoring-report` | 本文件所在提交 | PRD 与实施计划已冻结 |
| Phase 0 | 已完成 | `codex/m3-p0-contracts`、`codex/m3-phase0-contract-audit` | `443c396` | 四份严格契约、确定性 SVN Mock、55 项聚焦测试 |
| Phase 1 | 已完成 | `codex/m3-p1-diff-engine` | `9a52656` | 固定分支历史、最终净值、字段事件归因通过 |
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

### Phase 0

- 阶段分支：`codex/m3-p0-contracts`、`codex/m3-phase0-contract-audit`
- 验收提交：`443c396b9e205685008c14758f3dc5569d83d0b6`
- 验收结果：聚焦契约测试 55 passed；其余可收集契约回归 99 passed
- 冻结内容：四份严格 M3 契约及规范示例、确定性 SVN Mock、任务生命周期、Run/attempt、报告统计、未知与无法归因、左开右闭区间和仓库全局 Revision 间隙语义
- 已知限制：完整 `tests/contract` 因缺少 `config/settings.json` 在收集阶段中止；未创建占位配置

### Phase 1

- 阶段分支：`codex/m3-p1-diff-engine`
- 验收提交：`9a52656bfa35b76b31abc1be130ed91c338c6a3a`
- 验收结果：阶段聚焦测试及旧 Provider 回归 84 passed；直接相关 M2 清单、TableCsv 和 semantic diff 回归 29 passed
- 完成内容：面向固定 SVN 分支的只读 History Protocol 与 CLI 附加实现、严格目标分支路径和 UTC 左开右闭过滤、固定 Revision 快照最终净值计算，以及按 Revision 升序回放的字段事件归因
- 产品补充规则：`field_added`、`field_removed`、`field_definition_modified` 使用 `row_key=null`，且不计入 `changed_row_count`
- 已知限制：验收未真实访问 SVN；因缺少 `config/settings.json`，依赖该本机配置的测试未运行，且未创建占位配置
