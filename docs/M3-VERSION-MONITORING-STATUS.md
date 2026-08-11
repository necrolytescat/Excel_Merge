# M3 版本监控阶段状态

> 主控分支：`main`
> 更新日期：2026-08-11
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
| Phase 5 | 已完成 | `codex/m3-p5-monitor-ui` | `60e3fa8` | 严格 API、导航、新建页、任务列表与受控报告入口通过 |
| Phase 6 | 已完成 | `codex/m3-p6-real-acceptance` | `d466319`、`0f3a38d`、本文件所在提交 | 真实修改净值、Web 停服独立调度、锁屏触发、注销补跑和报告显示均已验收，M3 全阶段完成 |
| 报告体验优化 | 已完成 | `codex/m3-report-experience` | 本文件所在提交 | 按工作簿、Sheet、主键和字段组织的 Excel 式离线报告工作台通过 |

## 后续优化工作流

M3 功能验收完成后，后续优化拆成两个相互隔离的工作树，并从同一个 `main` 基线开始：

| 工作流 | 分支 | 目标 | 初始边界 |
|---|---|---|---|
| 报告页面改造 | `codex/m3-report-experience` | 从 QA 最终需要阅读、判断和回归的呈现效果反推信息结构 | 先使用脱敏离线报告 JSON 验证呈现；只有最终呈现确实缺少业务事实时，才提出新增对比或报告契约变更 |
| 性能瓶颈探索 | `codex/m3-report-performance` | 定位单次真实报告约 20 分钟的耗时构成，并形成可验证的优化候选 | 先测量和解释，不改变最终净值、归因、区间、报告契约或调度语义；输出当前对比逻辑的大白话说明 |

页面工作流的判断顺序：先明确 QA 要回答的问题，再设计报告层级与交互，然后核对现有 `m3.monitor-report.v1` 是否足够。不得为了页面效果先增加不明确的 SVN 读取或额外 Diff；确需新增业务事实时，必须先更新 PRD、契约、统计口径和验收样例。

性能工作流必须先记录各阶段耗时、SVN 调用数量、读取字节、工作簿数量、提交回放数量和结果哈希。任何优化候选都要证明同一输入仍得到相同的 Revision 区间、116 条基线变化、作者归因和错误覆盖，不能以跳过 manifest、固定 Revision 快照或字段事件回放换取速度。


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

### Phase 5

- 阶段分支：`codex/m3-p5-monitor-ui`
- 契约提交：`f47dfae`、`ebe7548`、`c742b61`
- 实现与返修提交：`c85464a`、`9f4a3ad`、`97fb0e5`、`56733a0`、`60e3fa8238d8d68f4c8865ec02215a40f648e619`
- 验收结果：最终 M3 Phase 0-5 聚焦 256 passed；排除 6 个本机配置依赖文件后的主控广泛回归 349 passed；前端/UI 复验 45 passed；后端 retry 最终专项 12 passed；JavaScript 语法、py_compile 与 git diff --check 通过
- 核心交付：左侧“版本监控”入口、任务创建与概览、二级任务列表和详情；任务筛选与 URL 恢复、状态和调度异常、Runner 心跳、遗漏数量、Run 与变化统计、生命周期操作、人工重试、latest 与历史报告入口；严格请求/响应/错误契约、分页游标、ETag/304、受控报告读取和安全响应头
- 幂等与恢复：schema v6 持久化所有写命令结果；相同 request ID 精确重放首次 201/202/4xx；retry 404、状态 409、并发 loser 与 202 outbox 同事务落账；未知异常保留 pending 并由启动恢复；事件驱动 dispatcher 按租约精确唤醒，不使用分钟级报告轮询
- 生命周期与性能：固定分支身份只由服务端冻结；归档与活动 retry 原子互斥，归档后 Runner 兜底拒绝；公开派生状态正确筛选；任务和 Run 使用 SQL 分页及批量摘要，避免全表扫描和 N+1；history 30 天后返回 410，受控 latest 在任务未彻底删除前保持可读
- 前端返修：断网或响应体中断时复用 request ID；自动刷新失败显示陈旧状态；筛选请求隔离旧响应；多页列表不自动收缩且请求不超过上限；过期报告不显示死链接；长文本和 360px 布局受控；动态业务文本使用安全 DOM API
- 已知限制：in-app 浏览器按技能流程由阶段任务和主控分别尝试，均被 Windows `CreateProcessWithLogonW failed: 1385` 阻断，因此未完成真实桌面/移动截图和视觉交互验收；本地 Mock 服务曾在 `127.0.0.1:5571` 正常监听，验收后已关闭；未真实访问 SVN；依赖缺失 `config/settings.json` 的 6 个测试文件未运行，且未创建占位配置

### Phase 6

- 阶段分支：`codex/m3-p6-real-acceptance`
- 已合入提交：`a8ff6cdb46c91cd5bc675e123c769f8fb79c08fb`、`49e960b90c19b4d0b5889b76660a53da62f4e3f2`、`d4663193a80a308cf500531776afa9fdd3e0356e`
- 真实调度修复：Windows 查询 XML 会省略默认 `Enabled=true`、`RunLevel=LeastPrivilege`，并把登录 SID 规范化为账户名；现改为按正式默认语义及 Windows SID 等价校验，同时继续拒绝显式禁用、提升权限、其他用户、额外触发器和错误每日周期
- 真实解析修复：分支工作簿包含 openpyxl 无法读取的重复渐变样式，OOXML fallback 在 Table 范围边界缺失时曾产生裸 `TypeError`；现仅对可证明安全的 row-only 范围恢复，空范围、缺行边界、越界或表外扩张均结构化失败，坏工作簿保持 partial 隔离
- 真实任务证据：固定分支 `KR_FIX_KR-Fix-1.0.1.0`，bound r26511、copy boundary r26215；现存 Windows 任务只读校验 `valid=True` 且无漂移，执行 scheduler-sync 后任务为 `active + synced`
- 首份报告证据：区间 `2026-08-10 19:50:00` 至 `19:55:00`（Asia/Shanghai），原 Run 复用人工重试成功；解析 197 个工作簿，0 个变化、0 个错误，latest 报告 HTTP 200，未创建重复区间
- Web 停服独立调度证据：一次性任务 `a6ee1c05-8a2c-5f75-b65c-cd76ecac1d11` 在 Web 端口已关闭时，于 `2026-08-10 21:20:01`（Asia/Shanghai）由 Windows 计划任务启动独立 Runner；`21:42:48` 完成后 Runner 正常退出，任务进入 `ended + not_present`，对应 Windows 任务已删除
- 真实修改报告证据：Run `dd754aac-c045-4337-b9f7-724e20cd3652` 覆盖 `(2026-08-10 18:45:00, 21:20:00]`，固定 Revision `r26475 -> r26514`；197 个工作簿全部可靠，2 个工作簿、9 个 Sheet、116 条最终净变化、0 个错误、0 个未知或无法归因项
- 净值与归因抽验：`r26509 / yilong` 形成 Base 行增删、LocalRank/Rank 的 `SeasonId 0 -> 101` 及 ArenaTop64 的 `ArenaPeakSeasonId 1001 -> 101`；`r26514 / yilong` 将 5 个 Sheet 加入 Excel `main` 导出清单，形成 28 个字段新增和 57 行新增。正式全仓重算与报告逐项一致，确认 116 条不是重复统计
- 恢复读取证据：独立 Runner 完成后重启 Web，任务最新报告 HTTP 200，报告历史 JSON/HTML 与 `latest.html` 均已发布且状态、Revision、统计一致
- 报告显示返修：真实报告数据完整但首版离线 HTML 的 JavaScript 换行转义错误，导致浏览器只显示静态表头；`0f3a38d` 修复新报告生成，并在不改写已发布历史文件和校验记录的前提下兼容展示旧报告。报告服务与 API 回归 55 passed，真实 116 条报告脚本通过 Node.js 语法检查
- 用户最终验收：2026-08-11 已确认锁屏触发、注销后登录补跑、视觉窗口和报告明细显示通过
- 自动化结果：Scheduler 52 passed；Manifest/M3 集成定向 21 passed；M2 相关 39 passed；M3 聚焦 256 passed；py_compile 与 git diff --check 通过
- 完成结论：真实 SVN 字段净值、作者与 Revision 归因，以及 Web/FastAPI 关闭后的独立调度均通过，Phase 6 与 M3 版本监控正式验收完成
- 已知限制：应用内浏览器自动化截图仍受 Windows `CreateProcessWithLogonW failed: 1385` 阻断；该工具限制不再阻断 M3 功能验收，真实视觉与交互由用户完成验收

### 报告体验优化

- 阶段分支：`codex/m3-report-experience`
- 验收提交：本文件所在提交
- 用户验收：2026-08-11 已确认脱敏样例的 Excel 式呈现、双向拖拽、新增/删除行列样例与删除项红色规则，并同意合入正式报告页
- 核心交付：左侧工作簿导航、顶部变化/错误 Sheet 页签、主键一行字段一列的变化网格、单元格前后值、最终修改人筛选，以及只显示最终修改人、Revision、修改时间的右侧归因栏
- 结构视觉：新增行为绿色；删除行和删除字段整行/整列为红色；字段定义变化为黄色；`field_added` 保留在报告 JSON 和统计中，但不单独生成结构列；Sheet 级结构事件保持 `row_key=null`，不伪造主键
- 验收返修：修复表格使用页面滚动时字段表头无法稳定置顶，以及新增字段结构按钮撑高并覆盖数据行；网格现为固定高度独立滚动区，字段表头和主键列分别冻结
- 交互验收：Edge 桌面和 390px 移动布局通过；工作簿/Sheet 切换、归因侧栏、鼠标上下左右拖拽、触控/键盘滚动及错误工作簿状态均已验证
- 自动化结果：M3 报告服务及直接相关回归 129 passed；Python 与 JavaScript 语法检查、`git diff --check` 通过
- 兼容与边界：旧报告仅在受控读取时基于内嵌 JSON 内存升级，不改写历史 HTML/JSON、SHA 或 publication；未增加 SVN 读取、额外 Diff 或契约字段，最终净值和字段归因语义不变
- 已知限制：`m3.monitor-report.v1` 只提供已比较工作簿总数，不提供无变化工作簿名称；左侧列出有变化或公开错误的工作簿，并显示其余已比较工作簿数量
