# M2-05 阶段 D1：批量 Diff 契约评审交接

> 状态：D1 已评审通过；D2 未开始
> 更新日期：2026-08-05
> 回归基线：`178 passed, 902 warnings in 8.26s`

## 1. D1 结论

本轮只完成批量 Diff 契约设计和冻结，没有实现生产调度器、批量 schema、API 路由、持久化服务或页面运行时。

D1 评审源：

- `docs/contracts/m2.batch.v1.md`：任务/单项模型、状态机、API、`result_ref`、持久化、清理、恢复、并发和失败隔离；
- `docs/contracts/m2.batch.v1.example.json`：包含四类 M1 候选及成功、业务失败、编排失败的完整终态示例；
- `docs/contracts/m2.batch.v1.acceptance.md`：D2 必须自动化的 57 个验收用例；
- `docs/ROADMAP.md`：M2/M3 归属冲突已解决。

## 2. 已冻结决策

1. 创建请求只接受两侧 `endpoint_id` 和具体冻结 Revision，不接受候选列表、路径、SVN URL、`HEAD` 或本地目录。
2. 服务端在准备阶段按 M1 规则重建完整权威候选集；浏览器候选不是输入或安全边界。
3. 任务状态为 `queued/preparing/running/cancelling/completed/completed_with_failures/cancelled/failed`。
4. 单项状态为 `queued/running/succeeded/business_failed/orchestration_failed/skipped/cancelled`。
5. `modified` 才调用现有单工作簿能力；`left_only/right_only/read_error` 直接 `skipped`。
6. `m2.diff.v1` 的 `partial/failed` 映射为批量 `business_failed`，但仍保存完整结果并生成 `result_ref`。
7. `result_ref` 为至少 128 bit 随机不透明引用；读取响应直接是原 `m2.diff.v1`。
8. 任务和结果保留 30 天，墓碑再保留 7 天；SQLite 元数据和独立 gzip JSON 支持重启恢复。
9. 全局工作簿并发为 2，单任务并发为 1，单项超时 600 秒；普通自动重试为 0，进程中断最多自动恢复 1 次。
10. 取消不强杀运行项；重试创建新任务，不修改原任务或原结果。
11. 首版单机可靠批量能力归 M2-07；通用/分布式队列、长期历史报告和管理能力归 M3。

## 3. 基线与修改范围

执行命令：

```powershell
py -3 -m pytest -q D:\Excel_Merge
```

结果：

```text
178 passed, 902 warnings in 8.26s
```

警告均为既有 FastAPI/Starlette 弃用警告。本轮只修改 `docs/`，未修改 `app/`、`core/`、`tests/` 或生产配置。

## 4. 评审重点

- 创建时由服务端重建全部 M1 候选，是否接受其准备成本；
- `partial/failed -> business_failed + 可读取 result_ref` 的分层是否符合报告口径；
- 取消不强杀、重试新建子任务是否满足审计要求；
- 30 天保留、全局并发 2、单任务并发 1、600 秒超时是否符合部署容量；
- 单机 SQLite + 文件结果是否满足 M2-07，分布式能力是否继续留在 M3。

## 5. 评审结论

D1 已于 2026-08-05 评审通过。D2 实施门禁已解除，但本次确认不自动启动 D2；开始 D2 时仍需重新读取工作区、运行全量测试，并以已冻结契约和验收用例为准。
