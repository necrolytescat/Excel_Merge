# M2-08 行与字段差异模块交接

> 状态：已验收并归档
> 归档日期：2026-08-09
> 阶段归属：M2-08，仍属于 M2
> 归档记录：`docs/archive/M2-08-ROW-FIELD-DIFF-ARCHIVE.md`
> 说明：第 1 至 8 节保留为历史启动上下文；后续状态以第 9 节和归档记录为准。

## 1. 下一阶段目标

下一对话只处理差异结果页中的“行与字段差异”模块。继续使用正式、Replay、Demo
共享的结果页实现，不重写一份正式版，也不建立第二套前端 Diff JSON。

每一项仍按固定流程执行：先用真实 Replay 展示当前行为和代表样本，分析原因，提出
最小方案、影响文件、风险和验收用例；等待用户明确确认后，只实施当前一项。

## 2. 已完成并锁定的区域

以下区域已由用户确认完成。下一对话不得修改其结构、文案、统计口径、样式或交互：

1. **M2 BATCH TASK**
   - 终态显示“无差异文件数”和“有差异文件数”；
   - 保留成功、业务失败、编排失败、跳过和取消等既有状态；
   - 对应 `compare_results_batch.js` 和批量面板 DOM 已锁定。
2. **工作簿导航**
   - 作为“差异结果”页面的左侧子导航，宽度 280px，独立纵向滚动；
   - 文件名不显示 `.xlsm/.xlsx`，字号 11px；
   - 标签显示 `+变化行数` 和 `-删除行数`；
   - 默认隐藏无变化工作簿，支持显示无变化；
   - 支持确认工作簿、确认后隐藏、显示已确认；
   - 该模块已明确锁定。
3. **比对结果标题区**
   - 标题为“比对结果”；
   - 不显示工作簿汇总串，不显示“当前工作簿”；
   - 已移除右侧“当前差异详情”模块；
   - 选中字段定位拼在标题说明中，格式为
     `Activity.xlsm · RookieSevenPeriod · 左侧第 12 行 / 右侧第 12 行`；
   - 标题内容和布局已锁定。
4. **Sheet 标题与导航**
   - 位于工作簿标题下方，横向排列并允许水平滚动；
   - 默认“显示修改”，可切换“显示全部”；
   - 标签单行显示 `Sheet名 +字段数 / -删除行数`；
   - 删除行数为 0 时不显示 `/ -0`；
   - 绿色字段数为 `modified_fields + target_only 新增行的全部字段数`；
   - 真实样本 `TroupeNew.xlsm / HeroVoice` 为 78 个新增行、每行 8 个字段，显示 `+624`；
   - 真实样本 `AtlasConfig.xlsm / TeamStar` 为 82 个修改字段、56 个新增字段、12 个删除行，显示 `+138 / -12`；
   - 失败 Sheet 显示红色“失败”；
   - Sheet 标题、筛选和标签已锁定。

如果行与字段模块的方案会影响上述区域，必须停止并向用户说明冲突，不能自行联动修改。

## 3. 当前行与字段模块

主要 DOM 和选择器：

- `.diff-main-pane`：行与字段区域容器；
- `.diff-toolbar`：模块标题和工作簿级字段数状态；
- `.semantic-table`、`#semantic-table-body`：差异行列表；
- `.semantic-diff-row`：单个差异行；
- `.semantic-key`：主键和值；
- `.row-change-status`：修改、右侧新增、左侧删除；
- `.field-diff-list`：当前行的字段列表；
- `.field-diff-button`：可选字段差异；
- `.field-diff-empty`：单侧整行没有可展示字段时的状态。

当前行为：

- 行按 mapper 输出顺序渲染；
- 行状态使用 `modified`、`added`、`deleted`；
- 修改行只显示真实字段变化；
- 单侧行显示该侧真实整行字段值；
- 首个可选字段自动选中；
- 点击字段只更新已锁定标题区中的 Sheet 和左右逻辑行定位；
- 右侧详情面板已删除，字段值仍保留在当前按钮中。

## 4. 真实 Replay 代表样本

只允许使用以下数据来源：

- `var/m2-fixtures/d3c-6e501824.m2fixture`；
- `tests/excel/left` 和 `tests/excel/right`；
- 现有契约示例与自动化测试数据。

推荐样本：

| 场景 | Replay 样本 | 当前数据特点 |
|---|---|---|
| 普通字段修改与定位 | `Activity.xlsm / RookieSevenPeriod` | 3 个修改字段，可验证左右逻辑行定位 |
| 长文本 | `ShopConfig.xlsm / RefreshShop` | `ShopId=510000` 的 `Key_RefreshDesc` 含长文本 |
| 混合修改、新增、删除 | `AtlasConfig.xlsm / TeamStar` | 82 修改字段、14 新增行、12 删除行 |
| 纯新增 Sheet | `TroupeNew.xlsm / HeroVoice` | 78 新增行、624 个新增字段 |
| 大结果 | `Loot.xlsm / Base` | 6027 个修改字段，验证滚动和渲染密度 |
| partial/error | `MainActivity.xlsm / FunctionName` | 已知夹具缺少 CSV，不得伪造行字段样本 |

如果用户指定的场景不在现有数据中，必须报告数据缺口并等待决定，不得制造业务数据。

## 5. 文件边界

下一阶段默认只允许在用户确认后修改：

- `app/static/compare_results.js` 中 `renderSheet`、`updateDetail` 及直接服务行字段展示的辅助函数；
- `app/static/compare_results_readability.css` 中行、字段、值和空状态相关选择器；
- `app/templates/compare_results.html` 中 `.diff-main-pane` 内部结构，确有必要时才修改；
- `tests/contract/test_compare_preview.py`；
- `tests/contract/test_diff_web_mapping.py`，仅当现有 view model 验收需要补充时。

即使位于同一文件，也不得改动：

- Batch Task 渲染和统计；
- 工作簿导航、确认状态和筛选；
- 比对结果标题、标题说明和定位格式；
- Sheet 导航、筛选、标签统计与失败状态；
- `app/static/m2_diff_mapper.js`，除非先证明现有 view model 无法表达需求并获得用户单独确认。

当前静态资源版本：

```text
app.css                          0.3.3
compare_results_readability.css 2.1.5
compare_results_batch.css       1.1.2
compare_results.js              2.1.5
compare_results_batch.js        1.2.1
offline_replay.js               1.1.0
```

## 6. 不可变约束

- 保持 `m2.diff.v1` 和 `m2.batch.v1`；
- 保持 `source=left`、`target=right`；
- Excel 与 CSV 必须来自同侧冻结 Revision；
- SVN 只读，不运行新正式任务；
- 不执行 Merge、Excel 写回或任何 SVN 写操作；
- 不修改 core 解析、配对、主键和 Diff 语义；
- 不新增第二套前端 Diff JSON；
- 正式结果页不得依赖 Demo 假数据；
- Replay 不更新黄金结果；
- 不访问其他业务数据。

服务端 `modified_fields` 继续保持原契约语义。Sheet 标签中的纯新增字段数只是现有 mapped
row 数据的展示层汇总，不回写契约，也不能被用于改变 Core Diff 统计。

## 7. 验收与测试

每项实施后至少执行：

```powershell
node --check app/static/compare_results.js
py -3 -m pytest -q tests/contract/test_compare_preview.py
py -3 -m pytest -q
git diff --check
```

当前基线为：

```text
212 passed, 1350 warnings
```

警告来自既有 FastAPI/Starlette 弃用提示和重复 ZIP 测试样本，不是当前前端失败。

## 8. 新对话启动提示词

```text
继续 M2-08，开始“行与字段差异”模块逐项改造，仍属于 M2。

工作目录：D:\Excel_Merge

先完整读取：
1. docs/M2-08-ROW-FIELD-DIFF-HANDOFF.md
2. docs/M2-08-DIFF-RESULT-FRONTEND-HANDOFF.md
3. docs/M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md
4. docs/M2-OFFLINE-FIXTURE-RUNBOOK.md
5. 当前 compare_results 模板、JS、CSS、mapper 和相关测试

先恢复上下文并报告状态，然后等待我指定第一项行与字段问题。不要自主设计或修改代码。

Batch Task、工作簿导航、比对结果标题区、Sheet 标题与导航已经锁定，不得修改。
每一项必须先使用真实 Replay 展示当前行为和代表样本，分析原因，提出最小方案、
影响文件、风险和验收用例，等待我明确确认后只实施当前一项。

保持 m2.diff.v1、m2.batch.v1、source=left/target=right、冻结 Revision、SVN 只读、
无 Merge/写回、不修改 core Diff 语义、不新增第二套前端 JSON，且正式页不依赖 Demo 数据。
```

## 9. 归档状态

行与字段差异模块已经按用户逐项确认完成，并于 2026-08-09 验收通过。最终实现包括
左右工作簿对照、四向拖拽、行与字段选择、修改状态循环导航、TARGET 字符级标红、
双行字段表头，以及“显示差异 / 显示原表”字段视图切换。

本交接不再作为“待启动”入口。实现目标、风险和逐项验收见
`M2-08-ROW-FIELD-SIDE-BY-SIDE-PLAN.md`，冻结结果与剩余 M2 边界见
`archive/M2-08-ROW-FIELD-DIFF-ARCHIVE.md`。
