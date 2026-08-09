# M2-05 阶段 D：批量 Diff 契约设计交接

> 状态：D1 已评审通过；D2 编排与 Web 链路已通过真实双分支试跑  
> 更新日期：2026-08-06  
> 当前基线：阶段 A/B/C/D1/D2 已完成，全量测试 `189 passed`

## 1. 交接结论

当前单工作簿纵向链路已经完成：

```text
冻结左右端点与 Revision
→ SVN 物化单工作簿及 main 映射 CSV
→ WorkbookDiffService
→ m2.diff.v1
→ 正式 /compare/results
```

阶段 D 的目标不是扩展 Diff 规则，而是把 M1 的全部文件级候选交给服务端后台任务逐项处理，并提供可恢复、可查询的批量结果。

```text
M1 文件级候选
→ 创建批量任务
→ 服务端逐项调度
→ 每个 modified 工作簿独立生成 m2.diff.v1
→ 批量层保存摘要与 result_ref
→ Web 按需加载单工作簿明细
```

阶段 D 必须先完成 D1 契约设计并评审，再实施 D2 运行时。不得让浏览器并发调用单工作簿接口来冒充批量任务。

## 2. 已完成基线

- `POST /api/diff/workbooks/compare` 已支持左右均存在的 `modified` 单工作簿；
- 成功响应直接使用冻结的 `m2.diff.v1`，没有第二套 Diff JSON；
- `partial/failed` 是合法 HTTP 200 业务结果；
- SVN 数据集使用请求中的具体 Revision，不重新读取 `HEAD`；
- 每侧只读取各自 `main.tbxName` 映射的 CSV；
- 请求级临时目录在成功和异常路径均清理；
- 正式结果页已展示真实单工作簿结果；
- 固定 AtlasConfig 结果为 16 Sheet、273 修改行、375 修改字段；
- 全量回归基线为 `178 passed`。

阶段 D 不得反向修改以上行为。

## 3. 阶段 D 输出

阶段 D 最终输出分为两层：

| 层 | 输出 | 作用 |
|---|---|---|
| 批量层 | 暂定 `m2.batch.v1` | 任务状态、进度、文件级结果、错误和 `result_ref` |
| 工作簿层 | 现有 `m2.diff.v1` | 单个工作簿的 Sheet、行和字段明细 |

批量结果不得内嵌所有工作簿的完整 `m2.diff.v1`。每个可用明细应独立保存，批量项只返回稳定 `result_ref`。

M1 候选的处理口径：

| 候选状态 | 批量层处理 |
|---|---|
| `modified` | 调用现有单工作簿能力，保存独立 `m2.diff.v1` |
| `left_only` | 保存文件级单侧状态，不调用语义 Diff |
| `right_only` | 保存文件级单侧状态，不调用语义 Diff |
| `read_error` | 保存文件级读取失败，不伪造成空差异 |

`m2.diff.v1.workbook.status=partial/failed` 仍表示引擎已经完成并生成合法业务结果。批量层必须把它与 SVN、调度、存储等编排失败分开。

## 4. D1 必须冻结的契约

新对话首先完成以下决策，不直接实现批量运行时：

1. **任务身份**：`task_id`、`request_id`、重复创建和幂等规则；
2. **输入身份**：左右 `endpoint_id`、冻结 Revision、候选来源及服务端重新校验方式；
3. **状态机**：任务和单项的合法状态、转换、终态与时间字段；
4. **进度口径**：总数、已处理、成功、业务失败、编排失败、跳过和取消如何统计；
5. **文件级模型**：`modified/left_only/right_only/read_error` 如何表达；
6. **结果引用**：`result_ref` 格式、读取权限、失效时间和不存在时的错误；
7. **持久化**：进程重启后的恢复要求、保存目录或存储介质、清理周期；
8. **并发与隔离**：服务端并发上限、单工作簿失败隔离、超时和重试；
9. **取消语义**：是否进入首版、已运行项如何保留、未运行项如何标记；
10. **API**：创建、查询任务、读取单项结果、取消或重试的路由与错误结构；
11. **Web 行为**：轮询或推送、刷新恢复、列表筛选、失败重试和按需加载；
12. **里程碑归属**：阶段 D 的任务队列能力归入 M2-07 还是按现有 Roadmap 归入 M3。

建议 D1 交付物：

- `docs/contracts/m2.batch.v1.md`；
- `docs/contracts/m2.batch.v1.example.json`；
- 批量 API 请求、响应和错误契约；
- 状态转换表；
- 持久化与清理决策；
- 契约验收用例清单。

这些文件名是建议，不是已经冻结的实现约束。

## 5. 已冻结约束

- `source=left`、`target=right`，不得根据时间推断 `old/new`；
- 不修改 `core/`、Excel/CSV 解析规则、语义 Diff 规则或 `m2.diff.v1`；
- 不扫描整个 `TableCsv` 猜测工作簿归属；
- 不重新读取 `HEAD`，Excel 与 CSV 必须来自同端点同冻结 Revision；
- 不执行任何 SVN 写操作，不修改原始 Excel 或 CSV；
- 不把单侧文件或读取失败伪造成 `diff_empty`；
- 单个工作簿失败不得终止其他工作簿；
- 批量层不复制或重写单工作簿 Diff 算法；
- 正式页面不使用 Demo 假数据；
- 浏览器不负责批量并发和任务可靠性。

以下后端文件继续视为冻结规则所有者：

- `core/workbook_manifest_parser.py`；
- `core/table_csv_parser.py`；
- `core/semantic_diff.py`；
- `core/m2_errors.py`；
- `app/services/workbook_diff_service.py`；
- `app/schemas/diff.py`。

## 6. D1 验收标准

- [ ] `m2.batch.v1` 的任务级与单项级字段完整且未知字段拒绝；
- [ ] 状态机不存在含糊终态或无法恢复的转换；
- [ ] 四类 M1 候选都有明确输出；
- [ ] `partial/failed` 与编排失败分层清楚；
- [ ] 进度统计在失败、跳过和取消场景下仍可计算；
- [ ] `result_ref` 不泄漏本地绝对路径或 SVN 凭据；
- [ ] API 不接受浏览器提交的本地目录、SVN URL 或 `HEAD`；
- [ ] 持久化、清理、重启恢复和并发上限有明确决定；
- [ ] 契约测试覆盖创建、查询、终态、部分失败、取消或不支持取消；
- [ ] Roadmap 中 M2/M3 的范围冲突已经显式解决；
- [ ] D1 评审通过前没有新增生产批量调度代码。

## 7. D2 实施顺序

D1 验收后才进入 D2：

1. 实现批量 schema、任务存储和服务端调度器；
2. 对 `modified` 项复用阶段 C 数据集物化和现有 Diff 服务；
3. 保存每个工作簿独立 `m2.diff.v1` 并生成 `result_ref`；
4. 实现任务查询、结果读取以及已冻结的取消/重试接口；
5. 接通主页面“比对差异”和结果页任务恢复；
6. 覆盖并发、失败隔离、重启恢复、清理和只读边界；
7. 运行全量回归，阶段 D 验收后停止。

D2 真实试跑已完成：54/54 候选进入终态，25 成功、29 业务失败、0 编排失败。批量层验收完成；真实数据兼容性问题不反向修改 `m2.batch.v1`，后续按 `M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md` 独立处理。

## 8. 新对话启动提示词

```text
执行 docs/M2-05-STAGE-D-BATCH-DIFF-HANDOFF.md 的阶段 D1。

开始前：
1. 读取 docs/README.md、docs/ROADMAP.md、
   docs/M2-BACKEND-STATUS-HANDOFF.md、
   docs/M2-05-WEB-DIFF-INTEGRATION-CONTRACT.md 和
   docs/M2-05-STAGE-D-BATCH-DIFF-HANDOFF.md。
2. 检查最新工作区并运行 py -3 -m pytest -q，记录真实基线；
   当前交接基线为 178 passed。
3. 以当前代码为准，不覆盖阶段 A/B/C 已完成的 Web、API 和 SVN 改造。

本次目标：
只设计并冻结 m2.batch.v1、批量任务状态机、API、持久化、
result_ref、失败隔离、并发和取消/重试规则，形成契约文档、
示例 JSON 和验收用例。

约束：
1. 本轮是 D1 契约设计，不实现生产批量调度器，不接页面运行时。
2. 每个 modified 工作簿继续独立使用现有 m2.diff.v1。
3. left_only/right_only/read_error 由批量层表达，不调用语义 Diff。
4. partial/failed 是已执行的合法业务结果，不等同于编排失败。
5. 不修改 core/、Excel/CSV 解析、语义 Diff 和 m2.diff.v1。
6. 不重新读取 HEAD，不扫描 TableCsv 猜归属，不执行 SVN 写操作。
7. 明确解决现有 Roadmap 将任务队列归入 M3 与阶段 D 范围的冲突。
8. D1 契约完成后停止，等待评审，不开始 D2。
```
