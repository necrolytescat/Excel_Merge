# M4 阶段交接

## 当前阶段

两次前端验收均已通过。阶段五治理、全量回归和最终交付已完成。

## 已完成

- 冻结 PRD、手册、ADR 和计划契约。
- 新增严格 Pydantic 计划与 TABLE 目录模型。
- 新增 SQLite 计划存储，包含创建、更新、归档、恢复、幂等和乐观锁。
- 新增 TABLE 工作簿目录和计划服务/API。
- 新增共享侧栏片段及 M4 页面、样式和原生 JavaScript。
- 已将 M4 服务、API 和页面路由装配到 `app/main.py`。
- 已将六个现有页面的重复侧栏替换为共享片段，并冻结 M2、M4、M3 导航顺序。
- 已补齐计划存储单元测试、计划 API 契约测试和共享侧栏契约测试。
- 全量自动化回归通过：`555 passed`。
- M4 三个 JavaScript 文件均通过 `node --check`。
- 新增 `tests/browser/m4_diff_plan_smoke.mjs`，通过真实 Edge 覆盖 1440px 与 390px 页面布局、导航顺序、高亮、SVN TABLE 加载、表格/目标选择及 Revision 控件。
- 浏览器检查确认桌面与窄屏无页面横向溢出、无控制台错误。
- 使用 5567 临时服务和隔离 SQLite 完成“仅保存、编辑、归档、恢复”页面生命周期检查；临时服务已关闭，验收服务使用 5566 和正式本地库。
- 第一次前端验收已通过。
- 新增 `m4.diff-plan-run.v1`、独立运行存储、冻结 Revision、调度恢复、取消和失败项重试。
- 哈希相同直接完成，哈希不同复用 M2 单工作簿执行器与原始 `m2.diff.v1`。
- 新增全部分支矩阵、目标分支页签和 URL 工作簿定位；M2 Mapper 与明细渲染器保持唯一。
- 阶段三、四窄范围集成回归：`56 passed`。
- 使用正式本地库完成真实 SVN 运行：计划 `d66bd460-7189-4071-97ac-be111cd383aa`，运行 `0e891dcb-980d-4633-b31b-e032c2c9399c`。
- 基准 `KR_FIX_KR-Fix-1.0.0.0` 与目标 `KR_FIX_KR-Fix-1.0.1.0` 均冻结为 r26579；10 张表格全部完成。
- 真实矩阵覆盖 6 个“有差异”、3 个“语义一致”、1 个“完全相同”，0 缺失、0 失败。
- `Activity.xlsm` 原始结果确认为 `m2.diff.v1`：6 个 Sheet、1 个变更 Sheet、3 行/3 字段差异。
- 新增 `tests/browser/m4_diff_plan_run_smoke.mjs`，覆盖 1440px 与 390px 的矩阵、目标分支页签、矩阵跳转、URL 刷新恢复和 M2 双栏字段明细。
- 浏览器回归发现并修复 M4 运行页 390px 的 Grid 最小内容宽度溢出；矩阵现在只在内部容器横向滚动，页面宽度保持 390px。
- 第二次前端人工验收已通过。
- 实现 30 天 gzip 明细物理清理：服务启动立即执行，默认每小时扫描，失败项下轮重试。
- 清理后保留计划、运行、矩阵、冻结 Revision、统计、`result_ref` 和重试关系；明细引用稳定返回 410。
- 增加损坏 gzip 的固定错误、清理异常脱敏、M4 数据目录与 SVN 缓存清理隔离。
- 阶段五重点治理测试：`17 passed`；最终全量回归：`566 passed`。
- 所有 M4/M2 前端 JavaScript 通过 `node --check`；真实运行页 1440px 与 390px 浏览器回归通过。
- `git diff --check -- ':!docs/ROADMAP.md'` 通过；未修改 M2 黄金夹具。

## 当前验收入口

- 运行结果：`http://127.0.0.1:5566/diff-plan-runs/0e891dcb-980d-4633-b31b-e032c2c9399c`
- 计划详情：`http://127.0.0.1:5566/diff-plans/d66bd460-7189-4071-97ac-be111cd383aa`

## 最终交付

- 服务地址：`http://127.0.0.1:5566`，当前阶段五进程 PID `58784`。
- 运行结果和计划详情继续使用上方真实验收入口。
- 维护入口：`docs/M4-DIFF-PLAN-HANDBOOK.md`。
- 正式契约：`docs/contracts/m4.diff-plan.v1.md`、`docs/contracts/m4.diff-plan-run.v1.md`。
- 不自动创建 Git 提交；当前变更保持在工作区供审查。

## 环境事件

2026-08-12 实施期间 Windows 沙箱一度对已有文件的补丁读取返回 `CreateProcessWithLogonW failed: 1385`；新增文件不受影响。继续工作时优先重试标准 `apply_patch`，不得用脚本覆写已有文件。

`git diff --check` 当前仅报告用户原有 `docs/ROADMAP.md:30` 的行尾空格；M4 实施未修改该文件，也不代为整理该变更。
