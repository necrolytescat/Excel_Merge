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
| Phase 2 | 已完成 | `codex/m3-p2-runner-store` | `f6afcb0` | SQLite、边界链、租约重试、独立 Runner 通过 |
| Phase 3 | 已完成 | `codex/m3-p3-report-lifecycle` | `2254229` | 离线 HTML、可恢复原子发布、latest 与 30 天治理通过 |
| Phase 4 | 已完成 | `codex/m3-p4-windows-scheduler` | `8c27a63` | Windows 计划任务、登录补跑、维护唤醒与公开失败链通过 |
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

### Phase 2

- 阶段分支：`codex/m3-p2-runner-store`
- 验收提交：`f6afcb0bb74805a5e7c3e71fdfe95dfd77b5557e`
- 验收结果：Phase 2 34 passed；P0/P1 77 passed；直接相关 M2 29 passed
- 核心交付：migration v2、逻辑边界幂等、pause/end final 恢复、自动重试最多 3 次、无 Web Runner
- 已知限制：因缺少 `config/settings.json`，依赖该本机配置的测试未运行，且未创建占位配置

### Phase 3

- 阶段分支：`codex/m3-p3-report-lifecycle`
- 验收提交：`c75d2be662c16ad4bbf319656aa904155372a6b9`、`2254229b7e20f5f7e215feddedf65145368580ef`
- 验收结果：M3 Phase 0-3 聚焦 164 passed；直接相关 M1/M2 回归 27 passed；排除 6 个本机配置依赖文件后的广泛回归 258 passed；py_compile 与 git diff --check 通过
- 核心交付：规范 JSON/HTML 与稳定 SHA、单文件离线筛选报告、注入防护、不透明引用与归属校验、同目录原子发布、可恢复 publication manifest、latest 单调推进、5 分钟租约续期、30 天隔离清理
- 验收返修：修复同秒不同微秒截止点文件名冲突、完全失败误覆盖 latest、unresolved-only partial 契约矛盾、确定性发布错误误重试、全任务保留治理和 SVN 公开错误分类
- 后续硬门禁：Phase 4 必须提供不依赖 Web 或活动监控任务的每日维护唤醒，负责触发全部任务的 30 天报告清理
- 已知限制：未真实访问 SVN；真实 in-app 浏览器视觉验收两次被 Windows `CreateProcessWithLogonW failed: 1385` 阻断；因缺少 `config/settings.json`，依赖该本机配置的 6 个测试文件未运行，且未创建占位配置

### Phase 4

- 阶段分支：`codex/m3-p4-windows-scheduler`
- 验收提交：`1428eba4322820a8b670ea642a4e95e26489213f`、`85de819a210f754c9a8cbdd178813144a14f35dc`、`8c27a631585f24d83ea2ec575a44a00d43db528a`
- 验收结果：Runner/Scheduler 定向 77 passed；M3 Phase 0-4 聚焦 205 passed；直接相关 M1/M2 回归 27 passed；排除 6 个本机配置依赖文件后的广泛回归 306 passed；最终独立复核 106 passed；py_compile 与 git diff --check 通过
- 核心交付：基于 `schtasks.exe` 结构化 XML 的每日触发、登录补跑和精确结束触发；System32 固定程序路径、当前 Windows Token SID、`InteractiveToken`/`LeastPrivilege`；generation 与同步状态 CAS；10 分钟最多 3 次系统重试；暂停/结束最终窗口两阶段收尾；不依赖 Web 或活动任务的每日维护唤醒；严格隔离的验收 CLI
- 验收返修：配置装配与到期物化失败进入公开失败链；确定性错误不触发 Windows 重试；未知瞬时异常脱敏并返回 exit 75；fallback 服从 `schedule_effective_at`、最终结束原子落库且按 generation 隔离；完整校验 Trigger Enabled、Principal、Action Context 和登录 SID 漂移
- 真实 Windows 验收：在隔离任务名、隔离临时数据库下完成 Create、XML 查询与验证、Run、maintenance 和 finally Delete；删除后 `exists=false` 且 System32 Query 确认任务不存在；临时数据库目录已删除，无孤立测试任务
- 已知限制：最终两次错误边界返修后未再次执行真实 `schtasks.exe` 写入验收，相关变更由 Fake/解析器与 CLI 回归覆盖；未真实访问 SVN；因缺少 `config/settings.json`，依赖该本机配置的 6 个测试文件未运行，且未创建占位配置
